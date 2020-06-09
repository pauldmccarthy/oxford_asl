#!/bin/env python
"""
Generates perfusion stats within various ROIs
"""
import os
import sys
import argparse
import csv
import glob

from fsl.data import atlases
from fsl.data.image import Image
import fsl.wrappers as fsl

import nibabel as nib
import numpy as np
import scipy.stats

# For GM/WM results we mask using GM/WM PVEs at the following thresholds
# (lower when we have separate PV corrected results)
PVE_THRESHOLD_PVC = 0.1
PVE_THRESHOLD_NOPVC = 0.8

class ArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser for program options
    """

    def __init__(self, **kwargs):
        argparse.ArgumentParser.__init__(self, prog="roi_stats", add_help=False, **kwargs)
        self.add_argument("--oxasl-output", required=True,
                          help="OXFORD_ASL or OXASL output directory")
        self.add_argument("--fslanat", required=True,
                          help="FSL_ANAT output directory - if not specified will try to use information from OXASL output directory")
        self.add_argument("--output", "-o", required=True,
                          help="Output directory")
        self.add_argument("--min-nvoxels", default=10, type=int,
                          help="Minimum number of relevant voxels required to report statistics")
        self.add_argument("--roi-native", nargs="*", default=[],
                          help="Additional ROI as binarised mask in ASL space. The name of the ROI will be the stripped filename. May be specified multiple times")
        self.add_argument("--roi-struct", nargs="*", default=[],
                          help="Additional ROI as binarised mask in structural space. The name of the ROI will be the stripped filename. May be specified multiple times")
        self.add_argument("--roi-mni", nargs="*", default=[],
                          help="Additional ROI as binarised mask in MNI space. The name of the ROI will be the stripped filename. May be specified multiple times")
        self.add_argument("--add-atlas-rois", action="store_true", default=False,
                          help="Add ROIs from Harvard-Oxford cortical/subcortical atlases")
        self.add_argument("--save-mni-rois", action="store_true", default=False,
                          help="Save ROIs in MNI space")
        self.add_argument("--save-native-rois", action="store_true", default=False,
                          help="Save ROIs in native (ASL) space")
        self.add_argument("--save-native-masks", action="store_true", default=False,
                          help="Save binary masks in native (ASL) space")

def _transform(img, warp, ref, premat=None, postmat=None, interp="trilinear", paddingsize=1, output_is_roi=False, output_roi_thresh=0.5):
    """
    Transform an image

    :param img: fsl.data.Image containing image to transform
    :param warp: Transformation matrix or warp image
    :param ref:  fsl.data.Image containing reference image
    :param premat: Optional Pre-warp affine transformation matrix
    :param premat: Optional Post-warp affine transformation matrix
    :param interp: Interpolation method
    :param paddingsize: Padding size in pixels
    :param output_is_roi: Output should be binarized as an ROI
    :param output_roi_threshold: Thresholding value for binarizing output ROI

    :return:  fsl.data.Image containing transformed image
    """
    kwargs = {
        "warp" : warp, 
        "premat" : premat, 
        "postmat" : postmat,
        "rel" : True,
        "super" : True,
        "superlevel" : "a",
        "interp" : interp,
        "paddingsize" : paddingsize,
    }
    ret = fsl.applywarp(img, ref, out=fsl.LOAD, **kwargs)["out"]

    if output_is_roi:
        # Binarise mask images
        ret = Image((ret.data > output_roi_threshold).astype(np.int), header=ret.header)
    return ret

def _write_nii(img, fname, header=None):
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    if isinstance(img, Image):
        img.save(fname)
    else:
        nii = nib.Nifti1Image(img, None, header=header)
        nii.update_header()
        nii.to_filename(fname)

def _addvar(f):
    """ Add an unused 'variance' parameter to a function which doesn't use it """
    def _f_with_var(val, var):
        return f(val)
    return _f_with_var

def mean_invvarweighted(val, var):
    """ Inverse variance weighted mean (i.e. precision weighted mean) """
    prec = 1 / var
    prec[~np.isfinite(prec)] = 0
    return np.sum(val * prec) / np.sum(prec)

def i2(val, var):
    """ I^2 Measure of heterogenaity """
    n = len(val)
    w = [(1/x) for x in var]
    mu_bar = sum(a*b for a,b in zip(w,val))/sum(w)

    #out = []
    Q = sum(a*b for a,b in zip(w,[(x - mu_bar)**2 for x in val]))
    #H = np.sqrt(Q/(n - 1))
    i2 = (Q-(n-1))/Q
    #tau2_DL = (Q - n + 1)/(sum(w) - sum([x**2 for x in w])/sum(w))
    #tau2_DL = max(0, tau2_DL)
    #out.extend([mu_bar,Q,H,I2,tau2_DL])

    #out = pd.DataFrame(out)
    #out['index']=['Weighted_Mean', 'Q', 'H', 'I2', 'tau2_DL']
    #out = out.set_index('index', drop=True)

    # Return I2 as an integer percentage
    return int(100*i2+0.5)

STATS_FNS = {
    "Mean" : _addvar(np.mean),
    "Std" : _addvar(np.std),
    "Median" : _addvar(np.median),
    "IQR" : _addvar(scipy.stats.iqr),
    "Precision-weighted mean" : mean_invvarweighted,
    "I2" : i2,
}

def get_stats(stats, img, var_img, roi, suffix="", ignore_nan=True, ignore_inf=True, min_nvoxels=10, mask=None):
    """
    Get a set of statistics for a 3D image within an roi

    :param img: 3D Numpy array
    :param roi: 3D Numpy array with same dimensions as img and boolean data type
    :param ignore_nan: Voxels with care NaN in img are ignored
    :param ignore_inf: Voxels which are infinite in img are ignored
    :param min_nvoxels: If the number of voxels in the ROI is less than this number
                       (after removing Nan and infinte values) no value will be returned

    :return: Mapping from name of statistic to value. This may be NaN or infinite depending
             on the input arguments. If the number of eligible voxels is less than min_nvoxels,
             None is returned (not NaN or zero).
    """
    if list(img.shape) != list(roi.shape):
        raise ValueError("Image must have same dimensions as ROI")
    if list(var_img.shape) != list(roi.shape):
        raise ValueError("Variance image must have same dimensions as ROI")
    if mask is not None and list(mask.shape) != list(roi.shape):
        raise ValueError("Mask must have same dimensions as ROI")

    effective_roi = roi
    if ignore_nan:
        effective_roi = np.logical_and(effective_roi, ~np.isnan(img))
    if ignore_inf:
        effective_roi = np.logical_and(effective_roi, np.isfinite(img))
    if mask is not None:
        effective_roi = np.logical_and(effective_roi, mask)

    sample_data = img[effective_roi]
    sample_var = var_img[effective_roi]
    nvoxels = len(sample_data)
    stats["Nvoxels" + suffix] = nvoxels
    for stat, fn in STATS_FNS.items():
        if nvoxels < min_nvoxels:
            stats[stat + suffix] = None
        else:
            stats[stat + suffix] = fn(sample_data, sample_var)

def add_native_roi(rois, roi, name, log=sys.stdout):
    """
    Add an ROI in native (ASL) space
    """
    rois.append({"name" : name, "mask_native" : roi.data})
    log.write(" - %s...DONE" % name)

def add_struct_roi(rois, roi, name, ref, struct2asl, threshold=0.5, log=sys.stdout):
    """
    Add an ROI in structural space
    """
    log.write(" - %s..." % name)
    roi_native = _transform(roi, warp=None, ref=ref, premat=struct2asl)
    rois.append({"name" : name, "roi_struct" : roi, "roi_native" : roi_native, "mask_native" : roi_native.data > threshold})
    log.write("DONE\n")

def add_mni_roi(rois, roi, name, mni2struc, ref, struct2asl, threshold=0.5, log=sys.stdout):
    """
    Add an ROI in MNI space
    """
    log.write(" - %s..." % name)
    roi_native = _transform(roi, mni2struc, ref=ref, postmat=struct2asl)
    rois.append({"name" : name, "roi_mni" : roi, "roi_native" : roi_native, "mask_native" : roi_native.data > threshold})
    log.write("DONE\n")
 
def add_rois_from_atlas(rois, mni2struc_warp, ref_img, struct2asl_mat, atlas_name, resolution=2, threshold=0.5, log=sys.stdout):
    """
    Get ROIs from an FSL atlas
    
    :param rois: Mapping from name to ROI array which will be updated
    :param mni2struc_warp: Warp image containing MNI->structural space warp
    :param struct2asl_mat: Matrix for struct->ASL transformation
    :param atlas_name: Name of the FSL atlas
    :param resolution: Resolution in mm
    :param threshold: Threshold for probabilistic atlases
    """
    log.write("\nAdding ROIs from standard atlas: %s (resolution=%imm, thresholding at %.2f)\n" % (atlas_name, resolution, threshold))
    registry = atlases.registry
    registry.rescanAtlases()
    desc = registry.getAtlasDescription(atlas_name)
    atlas = registry.loadAtlas(desc.atlasID, resolution=2)
    for label in desc.labels:
        add_mni_roi(rois, atlas.get(label=label), label.name, mni2struc_warp, ref_img, struct2asl_mat, threshold=50)
 
def get_perfusion_data(outdir, gm_pve_asl, wm_pve_asl, log=sys.stdout):
    perfusion_data = [
        {
            "suffix" : "", 
            "f" : Image(os.path.join(outdir, "perfusion_calib")), 
            "var" : Image(os.path.join(outdir, "perfusion_var_calib")),
            "mask" : None,
        },
    ]
    if os.path.isdir(os.path.join(outdir, "pvcorr")):
        log.write(" - Found partial volume corrected results - will mask ROIs using GM/WM PVE (threshold: %.2f)\n" % PVE_THRESHOLD_PVC)
        perfusion_data.extend([
            {
                "suffix" : " GM", 
                "f" : Image(os.path.join(outdir, "pvcorr", "perfusion_calib")), 
                "var" : Image(os.path.join(outdir, "pvcorr", "perfusion_var_calib")),
                "mask" : gm_pve_asl.data > PVE_THRESHOLD_PVC,
            },
            {
                "suffix" : " WM", 
                "f" : Image(os.path.join(outdir, "pvcorr", "perfusion_wm_calib")), 
                "var" : Image(os.path.join(outdir, "pvcorr", "perfusion_wm_var_calib")),
                "mask" : wm_pve_asl.data > PVE_THRESHOLD_PVC,
            },
        ])
    else:
        log.write(" - No partial volume corrected results - will mask ROIs using GM/WM PVE (threshold: %.2f)\n" % PVE_THRESHOLD_NOPVC)
        perfusion_data.extend([
            {
                "suffix" : " %i%%+GM" % (PVE_THRESHOLD_NOPVC*100), 
                "f" : Image(os.path.join(outdir, "perfusion_calib")), 
                "var" : Image(os.path.join(outdir, "perfusion_var_calib")),
                "mask" : gm_pve_asl.data > PVE_THRESHOLD_NOPVC,
            },
            {
                "suffix" : " %i%%+WM" % (PVE_THRESHOLD_NOPVC*100), 
                "f" : Image(os.path.join(outdir, "perfusion_calib")), 
                "var" : Image(os.path.join(outdir, "perfusion_var_calib")),
                "mask" : wm_pve_asl.data > PVE_THRESHOLD_NOPVC,
            },
        ])
    return perfusion_data

def main():
    options = ArgumentParser().parse_args()
    if options.oxasl_output is None:
        sys.stderr.write("oxford_asl output directory must be specified")
        sys.exit(1)
    if options.fslanat is None:
        sys.stderr.write("FSL_ANAT output directory must be specified")
        sys.exit(1)

    print("Regionwise analysis\n")
    print(" - Using oxford_asl output in %s" % options.oxasl_output)
    print(" - Using fsl_anat output in %s" % options.fslanat)

    # Get reference and transformation data from oxford_asl and fsl_anat output
    outdir = os.path.join(options.oxasl_output, "native_space")
    asl_ref = Image(os.path.join(outdir, "perfusion"))
    struc_ref = Image(os.path.join(options.fslanat, "T1"))
    gm_pve = Image(os.path.join(options.fslanat, "T1_fast_pve_1"))
    wm_pve = Image(os.path.join(options.fslanat, "T1_fast_pve_2"))
    struct2mni_warp = Image(os.path.join(options.fslanat, "T1_to_MNI_nonlin_coeff"))
    mni2struc_warp = fsl.invwarp(struct2mni_warp, struc_ref, out=fsl.LOAD)["out"]
    with open(os.path.join(outdir, "asl2struct.mat")) as asl2struct_file:
        asl2struct_mat = np.array([[float(v) for v in line.split()] for line in asl2struct_file.readlines()])
        struct2asl_mat = np.linalg.inv(asl2struct_mat)

    # Look for PVC or non-PVC results
    print("\nLoading perfusion images")
    gm_pve_asl = _transform(gm_pve, warp=None, ref=asl_ref, premat=struct2asl_mat)
    wm_pve_asl = _transform(wm_pve, warp=None, ref=asl_ref, premat=struct2asl_mat)
    perfusion_data = get_perfusion_data(outdir, gm_pve_asl, wm_pve_asl)

    rois = []
    print("\nLoading generic ROIs")
    for pv in (0.1, 0.8):
        add_struct_roi(rois, gm_pve, "%i%%+GM" % (pv*100), ref=asl_ref, struct2asl=struct2asl_mat, threshold=pv)
        add_struct_roi(rois, wm_pve, "%i%%+WM" % (pv*100), ref=asl_ref, struct2asl=struct2asl_mat, threshold=pv)

    # Add ROIs from command line
    print("\nLoading user-specified ROIs")
    for fname in options.roi_native:
        add_native_roi(rois, Image(fname), os.path.basename(fname).split(".")[0])
    for fname in options.roi_struct:
        add_struct_roi(rois, Image(fname), os.path.basename(fname).split(".")[0], asl_ref, struct2asl_mat)
    for fname in options.roi_mni:
        add_mni_roi(rois, Image(fname), os.path.basename(fname).split(".")[0], mni2struc_warp, asl_ref, struct2asl_mat)

    # Add ROIs from standard atlases
    if options.add_atlas_rois:
        add_rois_from_atlas(rois, mni2struc_warp, asl_ref, struct2asl_mat, "harvardoxford-cortical", threshold=0.5)
        add_rois_from_atlas(rois, mni2struc_warp, asl_ref, struct2asl_mat, "harvardoxford-subcortical", threshold=0.5)

    # Get stats in each ROI. Add name to stats dict to make TSV output easier
    print("\nGetting stats - minimum of %i voxels to report in region" % options.min_nvoxels)
    for roi in rois:
        roi["stats"] = {"name" : roi["name"]}
        for item in perfusion_data:
            get_stats(roi["stats"], item["f"].data, item["var"].data, roi["mask_native"], suffix=item["suffix"], mask=item["mask"], min_nvoxels=options.min_nvoxels)
    
    # Save output
    # Note we give TSV file a CSV extension to make Excel happier
    os.makedirs(options.output, exist_ok=True)
    writer = None
    with open(os.path.join(options.output, "region_analysis.csv"), mode="w", newline='') as tsv_file:
        for roi in rois:
            if writer is None:
                writer = csv.DictWriter(tsv_file, fieldnames=list(roi["stats"].keys()))
                writer.writeheader()

            writer.writerow(roi["stats"])            
            fname = roi["name"].replace(" ", "_").replace(",", "").lower() + ".nii.gz"
            if options.save_native_rois and "roi_native" in roi:
                _write_nii(roi["roi_native"], os.path.join(options.output, "rois_native", fname))
            if options.save_native_masks and "mask_native" in roi:
                _write_nii(roi["mask_native"], os.path.join(options.output, "masks_native", fname), header=asl_ref.header)
            if options.save_mni_rois and "roi_mni" in roi:
                _write_nii(roi["roi_mni"], os.path.join(options.output, "rois_mni", fname))

    print("\nDONE - Output in %s" % options.output)

if __name__ == "__main__":
    main()