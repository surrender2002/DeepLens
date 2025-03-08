"""
Automated lens design from scratch. This code uses RMS spot size for lens design, which is much faster than image-based lens design.

Technical Paper:
    Xinge Yang, Qiang Fu and Wolfgang Heidrich, "Curriculum learning for ab initio deep learned refractive optics," Nature Communications 2024.

This code and data is released under the Creative Commons Attribution-NonCommercial 4.0 International license (CC BY-NC.) In a nutshell:
    # The license is only for non-commercial use (commercial licenses can be obtained from authors).
    # The material is provided as-is, with no warranties whatsoever.
    # If you publish any code, data, or scientific work based on this, please cite our work.
"""

import torch
import os
import logging
import numpy as np
import yaml
import random
import string
from datetime import datetime
from tqdm import tqdm
from transformers import get_cosine_schedule_with_warmup
from deeplens import (
    GeoLens,
    DEPTH,
    WAVE_RGB,
    EPSILON,
    set_logger,
    set_seed,
    create_lens,
    create_video_from_images,
)


def config():
    """Config file for training."""
    # Config file
    with open("configs/2_auto_lens_design.yml") as f:
        args = yaml.load(f, Loader=yaml.FullLoader)

    # Result dir
    characters = string.ascii_letters + string.digits
    random_string = "".join(random.choice(characters) for i in range(4))
    current_time = datetime.now().strftime("%m%d-%H%M%S")
    exp_name = current_time + "-AutoLens-RMS-" + random_string
    result_dir = f"./results/{exp_name}"
    os.makedirs(result_dir, exist_ok=True)
    args["result_dir"] = result_dir

    if args["seed"] is None:
        seed = random.randint(0, 100)
        args["seed"] = seed
    set_seed(args["seed"])

    # Log
    set_logger(result_dir)
    logging.info(f"EXP: {args['EXP_NAME']}")

    # Device
    num_gpus = torch.cuda.device_count()
    args["num_gpus"] = num_gpus
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args["device"] = device
    logging.info(f"Using {num_gpus} {torch.cuda.get_device_name(0)} GPU(s)")

    # ==> Save config and original code
    with open(f"{result_dir}/config.yml", "w") as f:
        yaml.dump(args, f)

    with open(f"{result_dir}/2_autolens_rms.py", "w") as f:
        with open("2_autolens_rms.py", "r") as code:
            f.write(code.read())

    return args


def curriculum_design(
    self,
    lrs=[5e-4, 1e-4, 0.1, 1e-4],
    decay=0.02,
    iterations=5000,
    test_per_iter=100,
    importance_sampling=True,
    optim_mat=False,
    match_mat=False,
    result_dir="./results",
):
    """Optimize the lens by minimizing rms errors."""
    # Preparation
    depth = DEPTH
    num_grid = 15
    spp = 512

    shape_control = True
    centroid = False
    sample_rays_per_iter = 5 * test_per_iter if centroid else test_per_iter
    aper_start = self.surfaces[self.aper_idx].r * 0.4
    aper_final = self.surfaces[self.aper_idx].r

    if not logging.getLogger().hasHandlers():
        set_logger(result_dir)
    logging.info(
        f"lr:{lrs}, decay:{decay}, iterations:{iterations}, spp:{spp}, grid:{num_grid}."
    )

    optimizer = self.get_optimizer(lrs, decay, optim_mat=optim_mat)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer, num_warmup_steps=iterations // 10, num_training_steps=iterations
    )

    # Training
    pbar = tqdm(total=iterations + 1, desc="Progress", postfix={"rms": 0})
    for i in range(iterations + 1):
        # =====> Evaluate the lens
        if i % test_per_iter == 0:
            # Change aperture, curriculum learning
            aper_r = min(
                (aper_final - aper_start) * (i / iterations * 1.1) + aper_start,
                aper_final,
            )
            self.surfaces[self.aper_idx].r = aper_r
            self.fnum = self.foclen / aper_r / 2

            # Correct shape and evaluate
            if i > 0:
                if shape_control:
                    self.correct_shape()

                if optim_mat and match_mat:
                    self.match_materials()

            self.write_lens_json(f"{result_dir}/iter{i}.json")
            self.analysis(
                f"{result_dir}/iter{i}",
                zmx_format=True,
                plot_invalid=True,
                multi_plot=False,
            )

        # =====> Compute centriod and sample new rays
        if i % sample_rays_per_iter == 0:
            with torch.no_grad():
                # Sample rays
                scale = self.calc_scale_pinhole(depth)
                rays_backup = []
                for wv in WAVE_RGB:
                    ray = self.sample_point_source(
                        depth=depth,
                        num_rays=spp,
                        num_grid=num_grid,
                        wvln=wv,
                        importance_sampling=importance_sampling,
                    )
                    rays_backup.append(ray)

                # Calculate ray centers
                if centroid:
                    center_p = -self.psf_center(
                        point=ray.o[:, :, 0, :], method="chief_ray"
                    )
                    center_p = center_p.unsqueeze(-2).repeat(1, 1, spp, 1)
                else:
                    center_p = -self.psf_center(
                        point=ray.o[:, :, 0, :], method="pinhole"
                    )
                    center_p = center_p.unsqueeze(-2).repeat(1, 1, spp, 1)

        # =====> Optimize lens by minimizing rms
        loss_rms = []
        for j, wv in enumerate(WAVE_RGB):
            # Ray tracing
            ray = rays_backup[j].clone()
            ray, _ = self.trace(ray)
            xy = ray.project_to(self.d_sensor)
            xy_norm = (xy - center_p) * ray.ra.unsqueeze(-1)

            # Weight mask (L2 error)
            weight_mask = (xy_norm.clone().detach() ** 2).sum([-1, -2]) / (
                ray.ra.sum([-1]) + EPSILON
            )
            weight_mask /= weight_mask.mean()  # shape of [M, M]

            # Weighted L2 loss
            l_rms = torch.mean(xy_norm.abs().sum(-1).sum(-1) / (ray.ra.sum(-1) + EPSILON) * weight_mask)  
            loss_rms.append(l_rms)

        loss_rms = sum(loss_rms) / len(loss_rms)

        # Regularization
        loss_reg = self.loss_reg()
        w_reg = 0.1
        L_total = loss_rms + w_reg * loss_reg

        # Gradient-based optimization
        optimizer.zero_grad()
        L_total.backward()
        optimizer.step()
        scheduler.step()

        pbar.set_postfix(rms=loss_rms.item())
        pbar.update(1)

    pbar.close()


if __name__ == "__main__":
    args = config()
    result_dir = args["result_dir"]
    device = args["device"]

    # Bind function
    GeoLens.curriculum_design = curriculum_design

    # Create a lens
    lens = create_lens(
        foclen=args["foclen"],
        fov=args["fov"],
        fnum=args["fnum"],
        flange=args["flange"],
        thickness=args["thickness"],
        lens_type=args["lens_type"],
        save_dir=result_dir,
    )
    lens.set_target_fov_fnum(
        hfov=args["fov"] / 2 / 57.3,
        fnum=args["fnum"],
    )
    logging.info(
        f"==> Design target: focal length {round(args['foclen'], 2)}, diagonal FoV {args['fov']}deg, F/{args['fnum']}"
    )

    # =====> 2. Curriculum learning with RMS errors
    lens.curriculum_design(
        lrs=[float(lr) for lr in args["lrs"]],
        decay=float(args["decay"]),
        iterations=5000,
        test_per_iter=50,
        optim_mat=True,
        match_mat=False,
        result_dir=args["result_dir"],
    )

    # Need to train more for the best optical performance
    lens.optimize(
        lrs=[float(lr) for lr in args["lrs"]],
        decay=float(args["decay"]),
        iterations=5000,
        centroid=False,
        importance_sampling=True,
        optim_mat=True,
        match_mat=False,
        result_dir=args["result_dir"],
    )

    # =====> 3. Analyze final result
    lens.prune_surf(expand_surf=0.02)
    lens.post_computation()

    logging.info(
        f"Actual: diagonal FOV {lens.hfov}, r sensor {lens.r_sensor}, F/{lens.fnum}."
    )
    lens.write_lens_json(f"{result_dir}/final_lens.json")
    lens.analysis(save_name=f"{result_dir}/final_lens", zmx_format=True)

    # =====> 4. Create video
    create_video_from_images(f"{result_dir}", f"{result_dir}/autolens.mp4", fps=10)
