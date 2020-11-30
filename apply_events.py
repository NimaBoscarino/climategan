print("\n• Imports\n")
import time

import_time = time.time()
from omnigan.trainer import Trainer
from omnigan.data import is_image_file
from omnigan.utils import Timer
from omnigan.tutils import normalize
import skimage.io as io
from skimage.transform import resize
import argparse
from pathlib import Path
import numpy as np
from datetime import datetime

import_time = time.time() - import_time

XLA = False
try:
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met

    XLA = True
except ImportError:
    pass


def print_time(text, time_series, xla_purge_samples=-1):
    """
    Print a timeseries's mean and std with a label

    Args:
        text (str): label of the time series
        time_series (list): list of timings
    """
    if not time_series:
        return

    if xla_purge_samples > 0:
        time_series = time_series[xla_purge_samples:]

    m = np.mean(time_series)
    s = np.std(time_series)
    print(f"{text.capitalize() + ' ':.<26}  {m:.5f} +/- {s:.5f}")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-b",
        "--batch_size",
        type=int,
        default=8,
        help="Batch size to process input images to events",
    )
    parser.add_argument(
        "-i",
        "--images_paths",
        type=str,
        required=True,
        help="Path to a directory with image files",
    )
    parser.add_argument(
        "-o",
        "--output_path",
        type=str,
        default=None,
        help="Path to a directory were events should be written. "
        + "Will NOT write anything if this flag is not used.",
    )
    parser.add_argument(
        "-r",
        "--resume_path",
        type=str,
        default=None,
        help="Path to a directory containing the trainer to resume."
        + " In particular it must contain opts.yaml and checkpoints/",
    )
    parser.add_argument(
        "-t",
        "--time",
        action="store_true",
        default=False,
        help="Binary flag to time operations or not. Defaults to False.",
    )
    parser.add_argument(
        "-m",
        "--flood_mask_binarization",
        type=float,
        default=-1,
        help="Value to use to binarize masks (mask > value). "
        + "Set to -1 (default) to use soft masks (not binarized)",
    )
    parser.add_argument(
        "--half",
        action="store_true",
        default=False,
        help="Binary flag to use half precision (float16). Defaults to False.",
    )
    parser.add_argument(
        "-n",
        "--n_images",
        default=-1,
        type=int,
        help="Limit the number of images processed",
    )
    parser.add_argument(
        "-x",
        "--xla_purge_samples",
        type=int,
        default=-1,
        help="XLA compile time induces extra computations."
        + " Ignore -x samples when computing time averages",
    )

    return parser.parse_args()


if __name__ == "__main__":

    # ------------------------------------------------------
    # -----  Parse Arguments and initialize variables  -----
    # ------------------------------------------------------
    args = parse_args()
    print(
        "• Using args\n\n"
        + "\n".join(["{:25}: {}".format(k, v) for k, v in vars(args).items()]),
    )

    batch_size = args.batch_size
    half = args.half
    images_paths = Path(args.images_paths).expanduser().resolve()
    bin_value = args.flood_mask_binarization
    outdir = (
        Path(args.output_path).expanduser().resolve()
        if args.output_path is not None
        else None
    )
    resume_path = args.resume_path
    time_inference = args.time
    n_images = args.n_images
    xla_purge_samples = args.xla_purge_samples

    if outdir is not None:
        outdir.mkdir(exist_ok=True, parents=True)

    # -------------------------------
    # -----  Create time store  -----
    # -------------------------------
    stores = {}
    if time_inference:
        stores = {
            "encode": [],
            "depth": [],
            "segmentation": [],
            "mask": [],
            "wildfire": [],
            "smog": [],
            "flood": [],
            "numpy": [],
            "setup": [],
            "inference on all images": [],
            "write": [],
            "imports": [import_time],
        }

    # -------------------------------------
    # -----  Resume Trainer instance  -----
    # -------------------------------------
    print("\n• Initializing trainer\n")

    with Timer(store=stores.get("setup", [])):

        device = None
        if XLA:
            device = xm.xla_device()

        trainer = Trainer.resume_from_path(
            resume_path,
            setup=True,
            inference=True,
            new_exp=None,
            input_shapes=(3, 640, 640),
            device=device,
        )
        if half:
            trainer.G.half()

    # --------------------------------------------
    # -----  Read data from input directory  -----
    # --------------------------------------------
    print("\n• Reading Data\n")

    # find all images
    data_paths = [i for i in images_paths.glob("*") if is_image_file(i)]
    base_data_paths = data_paths
    # filter images
    if 0 < n_images < len(data_paths):
        data_paths = data_paths[:n_images]
    # repeat data
    elif n_images > len(data_paths):
        repeats = len(data_paths) // n_images + 1
        data_paths = base_data_paths * repeats
        data_paths = data_paths[:n_images]

    # read images to numpy arrays
    data = [io.imread(str(d)) for d in data_paths]
    # resize to standard input size 640 x 640
    data = [resize(d, (640, 640), anti_aliasing=True) for d in data]
    # normalize to -1:1
    data = [(normalize(d.astype(np.float32)) - 0.5) * 2 for d in data]
    # half precision
    # if half:
    #     data = [d.astype(np.float16) for d in data]

    n_batchs = len(data) // args.batch_size
    if len(data) % args.batch_size != 0:
        n_batchs += 1

    print("Found", len(base_data_paths), "images. Inferring on", len(data), "images.")

    # --------------------------------------------
    # -----  Batch-process images to events  -----
    # --------------------------------------------
    print(f"\n• Creating events on {str(trainer.device)}\n")

    all_events = []

    with Timer(store=stores.get("inference on all images", [])):
        for b in range(n_batchs):
            print(f"Batch {b + 1}/{n_batchs}", end="\r")
            images = data[b * batch_size : (b + 1) * batch_size]
            if not images:
                continue
            # concatenate images in a batch batch_size x height x width x 3
            images = np.stack(images)

            # Retreive numpy events as a dict {event: array}
            events = trainer.infer_all(
                images, True, stores, bin_value=bin_value, half=half, xla=XLA
            )

            # store events to write after inference loop
            all_events.append(events)
    print()

    # ----------------------------------------------
    # -----  Write events to output directory  -----
    # ----------------------------------------------
    if outdir:
        print("\n• Writing")
        with Timer(store=stores.get("write", [])):
            for b, events in enumerate(all_events):
                for i in range(len(list(events.values())[0])):
                    idx = b * batch_size + i
                    idx = idx % len(base_data_paths)
                    stem = Path(data_paths[idx]).stem
                    for event in events:
                        im_path = outdir / f"{stem}_{event}.png"
                        im_data = events[event][i]
                        io.imsave(im_path, im_data)

    # ---------------------------
    # -----  Print timings  -----
    # ---------------------------
    if time_inference:
        print("\n• Timings\n")
        for k in sorted(list(stores.keys())):
            print_time(k, stores[k], purge=xla_purge_samples)
    print()

    if XLA:
        with open(
            f"./omnigan_xla_metrics_{str(datetime.now()).replace(' ', '_')}.txt", "w"
        ) as f:
            report = met.metrics_report()
            print(report, file=f)