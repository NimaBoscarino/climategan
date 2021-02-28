"""
Compute metrics of the performance of the masker using a set of ground-truth labels

run eval_masker.py --model "/miniscratch/_groups/ccai/checkpoints/masker/victor/no_spade/msd (17)"

"""
print("Imports...", end="")
import os
import os.path
import traceback
from argparse import ArgumentParser
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from comet_ml import Experiment
import torch
import yaml
from skimage.color import rgba2rgb
from skimage.io import imread
from skimage.transform import resize
from torchvision.transforms import ToTensor

from omnigan.data import encode_mask_label
from omnigan.eval_metrics import (
    edges_coherence_std_min,
    get_confusion_matrix,
    masker_metrics,
    may_flood,
    missed_must,
    pred_cannot,
)
from omnigan.trainer import Trainer
from omnigan.utils import find_images

print("Ok.")


def uint8(array):
    return array.astype(np.uint8)


def crop_and_resize(image_path, label_path):
    """
    Resizes an image so that it keeps the aspect ratio and the smallest dimensions
    is 640, then crops this resized image in its center so that the output is 640x640
    without aspect ratio distortion

    Args:
        image_path (Path or str): Path to an image
        label_path (Path or str): Path to the image's associated label

    Returns:
        tuple((np.ndarray, np.ndarray)): (new image, new label)
    """

    img = imread(image_path)
    lab = imread(label_path)

    if img.shape[-1] == 4:
        img = uint8(rgba2rgb(img) * 255)

    if img.shape != lab.shape:
        print("\nWARNING: shape mismatch. Entering breakpoint to investigate:")
        breakpoint()

    # resize keeping aspect ratio: smallest dim is 640
    h, w = img.shape[:2]
    if h < w:
        size = (640, int(640 * w / h))
    else:
        size = (int(640 * h / w), 640)

    r_img = resize(img, size, preserve_range=True, anti_aliasing=True)
    r_img = uint8(r_img)

    r_lab = resize(lab, size, preserve_range=True, anti_aliasing=False, order=0)
    r_lab = uint8(r_lab)

    # crop in the center
    H, W = r_img.shape[:2]

    top = (H - 640) // 2
    left = (W - 640) // 2

    rc_img = r_img[top : top + 640, left : left + 640, :]
    rc_lab = r_lab[top : top + 640, left : left + 640, :]

    return rc_img, rc_lab


def parsed_args():
    """Parse and returns command-line args

    Returns:
        argparse.Namespace: the parsed arguments
    """
    parser = ArgumentParser()
    parser.add_argument(
        "--model", type=str, help="Path to a pre-trained model",
    )
    parser.add_argument(
        "--images_dir",
        default="/miniscratch/_groups/ccai/data/floodmasks_eval/imgs",
        type=str,
        help="Directory containing the original test images",
    )
    parser.add_argument(
        "--labels_dir",
        default="/miniscratch/_groups/ccai/data/floodmasks_eval/labels",
        type=str,
        help="Directory containing the labeled images",
    )
    parser.add_argument(
        "--preds_dir",
        default="/miniscratch/_groups/ccai/data/omnigan/flood_eval_inferred_masks",
        type=str,
        help="DEBUG: Directory containing pre-computed mask predictions",
    )
    parser.add_argument(
        "--image_size",
        default=640,
        type=int,
        help="The height and weight of the pre-processed images",
    )
    parser.add_argument(
        "--max_files", default=-1, type=int, help="Limit loaded samples",
    )
    parser.add_argument(
        "--bin_value", default=0.5, type=float, help="Mask binarization threshold"
    )
    parser.add_argument(
        "-y",
        "--yaml",
        default=None,
        type=str,
        help="load a yaml file to parametrize the evaluation",
    )
    parser.add_argument(
        "-t", "--tags", nargs="*", help="Comet.ml tags", default=[], type=str
    )
    parser.add_argument(
        "-p",
        "--plot",
        action="store_true",
        default=False,
        help="Plot masker images & their metrics overlays",
    )
    parser.add_argument(
        "--no_paint",
        action="store_true",
        default=False,
        help="Do not log painted images",
    )

    return parser.parse_args()


def plot_images(
    output_filename,
    img,
    label,
    pred,
    fp_map,
    fn_map,
    may_neg_map,
    may_pos_map,
    edge_coherence=-1,
    pred_edge=None,
    label_edge=None,
    dpi=300,
    alpha=0.5,
    vmin=0.0,
    vmax=1.0,
    fontsize="xx-small",
    cmap={
        "fp": "Reds",
        "fn": "Reds",
        "may_neg": "Oranges",
        "may_pos": "Purples",
        "pred": "Greens",
    },
):
    f, axes = plt.subplots(1, 5, dpi=dpi)

    # FPR (predicted mask on cannot flood)
    axes[0].imshow(img)
    fp_map_plt = axes[0].imshow(
        fp_map, vmin=vmin, vmax=vmax, cmap=cmap["fp"], alpha=alpha
    )
    axes[0].axis("off")
    axes[0].set_title("FPR: {:.4f}".format(fpr), fontsize=fontsize)

    # FNR (missed mask on must flood)
    axes[1].imshow(img)
    fn_map_plt = axes[1].imshow(
        fn_map, vmin=vmin, vmax=vmax, cmap=cmap["fn"], alpha=alpha
    )
    axes[1].axis("off")
    axes[1].set_title("FNR: {:.4f}".format(fnr), fontsize=fontsize)

    # May flood
    axes[2].imshow(img)
    if edge_coherence != -1:
        title = "MNR: {:.2f} | MPR: {:.2f}\nEdge coh.: {:.4f}".format(
            mnr, mpr, edge_coherence
        )
    #         alpha_here = alpha / 4.
    #         pred_edge_plt = axes[2].imshow(
    #             1.0 - pred_edge, cmap="gray", alpha=alpha_here
    #         )
    #         label_edge_plt = axes[2].imshow(
    #             1.0 - label_edge, cmap="gray", alpha=alpha_here
    #         )
    else:
        title = "MNR: {:.2f} | MPR: {:.2f}".format(mnr, mpr)
    #         alpha_here = alpha / 2.
    may_neg_map_plt = axes[2].imshow(
        may_neg_map, vmin=vmin, vmax=vmax, cmap=cmap["may_neg"], alpha=alpha
    )
    may_pos_map_plt = axes[2].imshow(
        may_pos_map, vmin=vmin, vmax=vmax, cmap=cmap["may_pos"], alpha=alpha
    )
    axes[2].set_title(title, fontsize=fontsize)
    axes[2].axis("off")

    # Prediction
    axes[3].imshow(img)
    pred_mask = axes[3].imshow(
        pred, vmin=vmin, vmax=vmax, cmap=cmap["pred"], alpha=alpha
    )
    axes[3].set_title("Predicted mask", fontsize=fontsize)
    axes[3].axis("off")

    # Labels
    axes[4].imshow(img)
    label_mask = axes[4].imshow(label, alpha=alpha)
    axes[4].set_title("Labels", fontsize=fontsize)
    axes[4].axis("off")

    f.savefig(
        output_filename,
        dpi=f.dpi,
        bbox_inches="tight",
        facecolor="white",
        transparent=False,
    )
    plt.close(f)


def get_inferences(image_arrays, model_path, paint=False, bin_value=0.5, verbose=0):
    """
    Obtains the mask predictions of a model for a set of images

    Parameters
    ----------
    image_arrays : array-like
        A list of (1, CH, H, W) images

    model_path : str
        The path to a pre-trained model

    Returns
    -------
    masks : list
        A list of (H, W) predicted masks
    """
    device = torch.device("cuda:0")
    torch.set_grad_enabled(False)
    to_tensor = ToTensor()
    xs = [to_tensor(array).unsqueeze(0) for array in image_arrays]
    xs = [x.to(torch.float32).to(device) for x in xs]
    xs = [(x - 0.5) * 2 for x in xs]
    trainer = Trainer.resume_from_path(
        model_path, inference=True, new_exp=None, device=device
    )
    masks = []
    painted = []
    for idx, x in enumerate(xs):
        if verbose > 0:
            print(idx, "/", len(xs), end="\r")
        m = trainer.G.mask(x=x)
        masks.append(m.squeeze().cpu())
        if paint:
            p = trainer.G.paint(m > bin_value, x)
            painted.append(p.squeeze().cpu())
    return masks, painted


if __name__ == "__main__":
    # -----------------------------
    # -----  Parse arguments  -----
    # -----------------------------
    args = parsed_args()
    print("Args:\n" + "\n".join([f"    {k:20}: {v}" for k, v in vars(args).items()]))

    # Determine output dir
    try:
        tmp_dir = Path(os.environ["SLURM_TMPDIR"])
    except Exception as e:
        print(e)
        tmp_dir = input("Enter tmp output directory: ")
    plot_dir = tmp_dir.joinpath("plots")
    plot_dir.mkdir(parents=True, exist_ok=True)

    # Build paths to data
    imgs_paths = sorted(find_images(args.images_dir, recursive=False))
    labels_paths = sorted(find_images(args.labels_dir, recursive=False))
    if args.max_files > 0:
        imgs_paths = imgs_paths[: args.max_files]
        labels_paths = labels_paths[: args.max_files]

    print(f"Loading {len(imgs_paths)} images and labels...")

    # Pre-process images: resize + crop
    # TODO: ? make cropping more flexible, not only central
    ims_labs = [crop_and_resize(i, l) for i, l in zip(imgs_paths, labels_paths)]
    imgs = [d[0] for d in ims_labs]
    labels = [d[1] for d in ims_labs]
    print(" Done.")

    # Encode labels
    print("Encode labels...", end="", flush=True)
    # HW label
    labels = [np.squeeze(encode_mask_label(label, "flood")) for label in labels]
    print("Done.")

    if args.yaml:
        y_path = Path(args.yaml)
        assert y_path.exists()
        assert y_path.suffix in {".yaml", ".yml"}
        with y_path.open("r") as f:
            data = yaml.safe_load(f)
        assert "models" in data

        evaluations = [m for m in data["models"]]
    else:
        evaluations = [args.model]

    for e, eval_path in enumerate(evaluations):
        print("\n>>>>> Evaluation", e, ":", eval_path)
        print("=" * 50)
        print("=" * 50)

        # Initialize New Comet Experiment
        exp = Experiment(project_name="omnigan-masker-metrics", display_summary_level=0)

        # Obtain mask predictions
        print("Obtain mask predictions", end="", flush=True)

        preds, painted = get_inferences(
            imgs,
            eval_path,
            paint=not args.no_paint,
            bin_value=args.bin_value,
            verbose=1,
        )
        preds = [pred.numpy() for pred in preds]
        print(" Done.")

        if args.bin_value > 0:
            preds = [pred > args.bin_value for pred in preds]

        # Compute metrics
        df = pd.DataFrame(
            columns=[
                "fpr",
                "fnr",
                "mnr",
                "mpr",
                "tpr",
                "tnr",
                "precision",
                "f1",
                "edge_coherence",
                "filename",
            ]
        )

        print("Compute metrics and plot images", end="", flush=True)
        for idx, (img, label, pred) in enumerate(zip(*(imgs, labels, preds))):
            print(idx, "/", len(imgs), end="\r")

            # Basic metrics
            fp_map, fpr = pred_cannot(pred, label, label_cannot=0)
            fn_map, fnr = missed_must(pred, label, label_must=1)
            may_neg_map, may_pos_map, mnr, mpr = may_flood(pred, label, label_may=2)
            tpr, tnr, precision, f1 = masker_metrics(
                pred, label, label_cannot=0, label_must=1
            )

            # Edges coherence
            edge_coherence, pred_edge, label_edge = edges_coherence_std_min(pred, label)

            series_dict = {
                "fpr": fpr,
                "fnr": fnr,
                "mnr": mnr,
                "mpr": mpr,
                "tpr": tpr,
                "tnr": tnr,
                "precision": precision,
                "f1": f1,
                "edge_coherence": edge_coherence,
                "filename": os.path.basename(imgs_paths[idx]),
            }
            df.loc[idx] = pd.Series(series_dict)

            for k, v in series_dict.items():
                if k == "filename":
                    continue
                exp.log_metric(f"img_{k}", v, step=idx)

            # Confusion matrix
            confmat, _ = get_confusion_matrix(tpr, tnr, fpr, fnr, mpr, mnr)
            confmat = np.around(confmat, decimals=3)
            exp.log_confusion_matrix(
                file_name=Path(imgs_paths[idx].name + ".json"),
                title=imgs_paths[idx].name,
                matrix=confmat,
                labels=["Cannot", "Must", "May"],
                row_label="Predicted",
                column_label="Ground truth",
            )

            if args.plot:
                # Plot prediction images
                fig_filename = plot_dir.joinpath(imgs_paths[idx].name)
                plot_images(
                    fig_filename,
                    img,
                    label,
                    pred,
                    fp_map,
                    fn_map,
                    may_neg_map,
                    may_pos_map,
                    edge_coherence,
                    pred_edge,
                    label_edge,
                )
                exp.log_image(fig_filename)
            if not args.no_paint:
                p = ((painted[idx].permute(1, 2, 0).numpy() + 1) / 2 * 255).astype(
                    np.uint8
                )
                masked = img * (1 - pred[..., None])
                combined = np.concatenate([masked, p], 1)
                exp.log_image(combined, Path(imgs_paths[idx]).name)

        print(" Done.")
        try:
            # Summary statistics
            means = df.mean(axis=0)
            confmat_mean, confmat_std = get_confusion_matrix(
                df.tpr, df.tnr, df.fpr, df.fnr, df.mpr, df.mnr
            )
            confmat_mean = np.around(confmat_mean, decimals=3)
            confmat_std = np.around(confmat_std, decimals=3)

            # Log to comet
            exp.log_confusion_matrix(
                file_name="confusion_matrix_mean.json",
                title="confusion_matrix_mean.json",
                matrix=confmat_mean,
                labels=["Cannot", "Must", "May"],
                row_label="Predicted",
                column_label="Ground truth",
            )
            exp.log_confusion_matrix(
                file_name="confusion_matrix_std.json",
                title="confusion_matrix_std.json",
                matrix=confmat_std,
                labels=["Cannot", "Must", "May"],
                row_label="Predicted",
                column_label="Ground truth",
            )
            exp.log_metrics(dict(means))
        except Exception as e:
            print(f"\n\n{e}\n{traceback.format_exc()}\n\n")
            exp.log_parameter("ABORTED", True)
        finally:
            exp.log_table("metrics.csv", df)
            exp.log_html(df.to_html(col_space="80px"))
            exp.log_parameters(vars(args))
            exp.log_parameters(eval_path)
            exp.add_tag("eval_masker")
            if args.tags:
                exp.add_tags(args.tags)
            exp.log_parameter("model_name", Path(eval_path).name)
            exp.end()
