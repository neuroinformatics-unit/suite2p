"""
Copyright © 2023 Howard Hughes Medical Institute, Authored by Carsen Stringer and Marius Pachitariu.
"""
import numpy as np
from typing import Any, Dict
from scipy.ndimage import find_objects, gaussian_filter
from cellpose.models import CellposeModel
from cellpose import transforms, dynamics, core
from cellpose.utils import fill_holes_and_remove_small_masks
from cellpose.transforms import normalize99
import time
import cv2
import os

from . import utils
from .stats import roi_stats
from .detect import select_rois


def mask_centers(masks):
    centers = np.zeros((masks.max(), 2), np.int32)
    diams = np.zeros(masks.max(), np.float32)
    slices = find_objects(masks)
    for i, si in enumerate(slices):
        if si is not None:
            sr, sc = si
            ymed, xmed, diam = utils.mask_stats(masks[sr, sc] == (i + 1))
            centers[i] = np.array([ymed, xmed])
            diams[i] = diam
    return centers, diams


def patch_detect(patches, diam):
    """ anatomical detection of masks from top active frames for putative cell """
    print("refining masks using cellpose")
    npatches = len(patches)
    ly = patches[0].shape[0]
    model = CellposeModel(gpu=True if core.use_gpu() else False)
    imgs = np.zeros((npatches, ly, ly, 2), np.float32)
    for i, m in enumerate(patches):
        imgs[i, :, :, 0] = transforms.normalize99(m)
    rsz = 30. / diam
    imgs = transforms.resize_image(imgs, rsz=rsz).transpose(0, 3, 1, 2)
    imgs, ysub, xsub = transforms.pad_image_ND(imgs)

    pmasks = np.zeros((npatches, ly, ly), np.uint16)
    batch_size = 8 * 224 // ly
    tic = time.time()
    for j in np.arange(0, npatches, batch_size):
        # Maintain compatibility with both Cellpose 3 and 4
        # Use try-except instead of hasattr for Numba compatibility
        try:
            # Try Cellpose 4 first
            y = model.net(imgs[j:j + batch_size])[0]
        except AttributeError:
            try:
                # Try Cellpose 3
                y = model.cp.network(imgs[j:j + batch_size])[0]
            except AttributeError:
                raise AttributeError("Could not find network attribute in Cellpose model - unsupported Cellpose version")
        
        y = y[:, :, ysub[0]:ysub[-1] + 1, xsub[0]:xsub[-1] + 1]
        y = y.asnumpy()
        for i, yi in enumerate(y):
            cellprob = yi[-1]
            dP = yi[:2]
            niter = 1 / rsz * 200
            p = dynamics.follow_flows(-1 * dP * (cellprob > 0) / 5., niter=niter)
            maski = dynamics.get_masks(p, iscell=(cellprob > 0), flows=dP,
                                       threshold=1.0)
            maski = fill_holes_and_remove_small_masks(maski)
            maski = transforms.resize_image(maski, ly, ly,
                                            interpolation=cv2.INTER_NEAREST)
            pmasks[j + i] = maski
        if j % 5 == 0:
            print("%d / %d masks created in %0.2fs" %
                  (j + batch_size, npatches, time.time() - tic))
    return pmasks


def refine_masks(stats, patches, seeds, diam, Lyc, Lxc):
    nmasks = len(patches)
    patch_masks = patch_detect(patches, diam)
    ly = patches[0].shape[0] // 2
    igood = np.zeros(nmasks, "bool")
    for i, (patch_mask, stat, (yi, xi)) in enumerate(zip(patch_masks, stats, seeds)):
        mask = np.zeros((Lyc, Lxc), np.float32)
        ypix0, xpix0 = stat["ypix"], stat["xpix"]
        mask[ypix0, xpix0] = stat["lam"]
        func_mask = utils.square_mask(mask, ly, yi, xi)
        ious = utils.mask_ious(patch_mask.astype(np.uint16), (func_mask
                                                              > 0).astype(np.uint16))[0]
        if len(ious) > 0 and ious.max() > 0.45:
            mask_id = np.argmax(ious) + 1
            patch_mask = patch_mask[max(0, ly - yi):min(2 * ly, Lyc + ly - yi),
                                    max(0, ly - xi):min(2 * ly, Lxc + ly - xi)]
            func_mask = func_mask[max(0, ly - yi):min(2 * ly, Lyc + ly - yi),
                                  max(0, ly - xi):min(2 * ly, Lxc + ly - xi)]
            ypix0, xpix0 = np.nonzero(patch_mask == mask_id)
            lam0 = func_mask[ypix0, xpix0]
            lam0[lam0 <= 0] = lam0.min()
            ypix0 = ypix0 + max(0, yi - ly)
            xpix0 = xpix0 + max(0, xi - ly)
            igood[i] = True
            stat["ypix"] = ypix0
            stat["xpix"] = xpix0
            stat["lam"] = lam0
            stat["anatomical"] = True
        else:
            stat["anatomical"] = False
    return stats


def roi_detect(ops, mproj, mov, diameter=None, cellprob_threshold=0.0, flow_threshold=0.4,
               pretrained_model=None):
    pretrained_model = "cpsam" if pretrained_model is None else pretrained_model
    # If diameter is 0, set to None for Cellpose automatic estimation
    if diameter == 0:
        diameter = None
    model = CellposeModel(pretrained_model=pretrained_model, gpu=True if core.use_gpu() else False)
    # Call model.eval and handle both 3 and 4 return values for compatibility
    eval_result = model.eval(mproj, diameter=diameter,
                       cellprob_threshold=cellprob_threshold,
                       flow_threshold=flow_threshold)
    
    if len(eval_result) == 4:
        masks, flows, styles, diams = eval_result
        if isinstance(diams, (list, np.ndarray)):
            median_diam = np.median(diams)
        else:
            median_diam = diams
    else:
        masks, flows, styles = eval_result
        print(f"Estimating diameter from activity-based detection")
        median_diam = estimate_diameter_from_activity(ops, mov)
    
    shape = masks.shape
    _, masks = np.unique(np.int32(masks), return_inverse=True)
    masks = masks.reshape(shape)
    centers, mask_diams = mask_centers(masks)
    if median_diam is not None:
        print(">>>> %d masks detected, median diameter = %0.2f " %
              (masks.max(), median_diam))
    else:
        print(">>>> %d masks detected, median diameter = None (estimation failed)" %
              masks.max())
    return masks, centers, median_diam, mask_diams.astype(np.int32)


def masks_to_stats(masks, weights):
    stats = []
    slices = find_objects(masks)
    for i, si in enumerate(slices):
        sr, sc = si
        ypix0, xpix0 = np.nonzero(masks[sr, sc] == (i + 1))
        ypix0 = ypix0.astype(int) + sr.start
        xpix0 = xpix0.astype(int) + sc.start
        ymed = np.median(ypix0)
        xmed = np.median(xpix0)
        imin = np.argmin((xpix0 - xmed)**2 + (ypix0 - ymed)**2)
        xmed = xpix0[imin]
        ymed = ypix0[imin]
        stats.append({
            "ypix": ypix0,
            "xpix": xpix0,
            "lam": weights[ypix0, xpix0],
            "med": [ymed, xmed],
            "footprint": 1
        })
    stats = np.array(stats)
    return stats


def select_rois(ops: Dict[str, Any], mov: np.ndarray, diameter=None):
    """ find ROIs in static frames
    
    Parameters:

        ops: dictionary
            requires keys "high_pass", "anatomical_only", optional "yrange", "xrange"
        
        mov: ndarray t x Lyc x Lxc, binned movie
    
    Returns:
        stats: list of dicts
    
    """
    Lyc, Lxc = mov.shape[1:]
    mean_img = mov.mean(axis=0)
    mov = utils.temporal_high_pass_filter(mov=mov, width=int(ops["high_pass"]))
    max_proj = mov.max(axis=0)
    #max_proj = np.percentile(mov, 90, axis=0) #.mean(axis=0)
    if ops["anatomical_only"] == 1:
        img = np.log(np.maximum(1e-3, max_proj / np.maximum(1e-3, mean_img)))
        weights = max_proj
    elif ops["anatomical_only"] == 2:
        img = mean_img
        weights = 0.1 + np.clip(
            (mean_img - np.percentile(mean_img, 1)) /
            (np.percentile(mean_img, 99) - np.percentile(mean_img, 1)), 0, 1)
    elif ops["anatomical_only"] == 3:
        if "meanImgE" in ops:
            img = ops["meanImgE"][ops["yrange"][0]:ops["yrange"][1],
                                  ops["xrange"][0]:ops["xrange"][1]]
        else:
            img = mean_img
            print("no enhanced mean image, using mean image instead")
        weights = 0.1 + np.clip(
            (mean_img - np.percentile(mean_img, 1)) /
            (np.percentile(mean_img, 99) - np.percentile(mean_img, 1)), 0, 1)
    else:
        img = max_proj.copy()
        weights = max_proj

    t0 = time.time()
    if diameter is not None:
        if isinstance(diameter, (list, np.ndarray)) and len(ops["diameter"]) > 1:
            rescale = diameter[1] / diameter[0]
            img = cv2.resize(img, (Lxc, int(Lyc * rescale)))
        else:
            rescale = 1.0
            diameter = [diameter, diameter]
        if diameter[1] > 0:
            print("!NOTE! diameter set to %0.2f for cell detection with cellpose" %
                  diameter[1])
        else:
            print(
                "!NOTE! diameter set to 0 or None, diameter will be estimated by cellpose if possible"
            )
    else:
        print(
            "!NOTE! diameter set to 0 or None, diameter will be estimated by cellpose if possible")

    if ops.get("spatial_hp_cp", 0):
        img = np.clip(normalize99(img), 0, 1)
        img -= gaussian_filter(img, diameter[1] * ops["spatial_hp_cp"])

    masks, centers, median_diam, mask_diams = roi_detect(
        ops, img, mov, diameter=diameter[1], flow_threshold=ops["flow_threshold"],
        cellprob_threshold=ops["cellprob_threshold"],
        pretrained_model=ops["pretrained_model"])
    if rescale != 1.0:
        masks = cv2.resize(masks, (Lxc, Lyc), interpolation=cv2.INTER_NEAREST)
        img = cv2.resize(img, (Lxc, Lyc))
    stats = masks_to_stats(masks, weights)
    print("Detected %d ROIs, %0.2f sec" % (len(stats), time.time() - t0))

    new_ops = {
        "diameter": median_diam,
        "max_proj": max_proj,
        "Vmax": 0,
        "ihop": 0,
        "Vsplit": 0,
        "Vcorr": img,
        "Vmap": 0,
        "spatscale_pix": 0
    }
    ops.update(new_ops)

    return stats


def estimate_diameter_from_activity(ops, mov):
    """Estimate diameter using activity-based detection (anatomical_only == 0)."""
    
    
    ops_copy = ops.copy()
    ops_copy["anatomical_only"] = 0
    try:
        # Use the full movie for activity-based detection
        stat = select_rois(ops_copy, mov)
        if len(stat) > 0:
            # Estimate diameter for each ROI
            diams = []
            for s in stat:
                mask = np.zeros((mov.shape[1], mov.shape[2]), dtype=bool)
                mask[s["ypix"], s["xpix"]] = True
                _, _, diam = utils.mask_stats(mask)
                diams.append(diam)
            median_diam = np.median(diams)
            return median_diam
        else:
            print("Activity-based diameter estimation failed: no ROIs were found -- check registered binary and maybe change spatial scale")
            return None
    except Exception as e:
        print(f"Activity-based diameter estimation failed: {e}")
        return None


# def run_assist():
#     nmasks, diam = 0, None
#     if anatomical:
#         try:
#             print(">>>> CELLPOSE estimating spatial scale and masks as seeds for functional algorithm")
#             from . import anatomical
#             mproj = np.log(np.maximum(1e-3, max_proj / np.maximum(1e-3, mean_img)))
#             masks, centers, diam, mask_diams = anatomical.roi_detect(mproj)
#             nmasks = masks.max()
#         except:
#             print("ERROR importing or running cellpose, continuing without anatomical estimates")
#         if tj < nmasks:
#             yi, xi = centers[tj]
#             ls = mask_diams[tj]
#             imap = np.ravel_multi_index((yi, xi), (Lyc, Lxc))
# if nmasks > 0:
#         stats = anatomical.refine_masks(stats, patches, seeds, diam, Lyc, Lxc)
#         for stat in stats:
#             if stat["anatomical"]:
#                 stat["lam"] *= sdmov[stat["ypix"], stat["xpix"]]
