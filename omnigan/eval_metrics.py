import numpy as np
import cv2
import torch

# ------------------------------------------------------------------------------
# ----- Evaluation metrics for a pair of binary mask images (pred, target) -----
# ------------------------------------------------------------------------------


def get_accuracy(arr1, arr2):
    """pixel accuracy

    Args:
        arr1 (np.array)
        arr2 (np.array)
    """
    return (arr1 == arr2).sum() / arr1.size


def trimap(pred_im, gt_im, thickness=8):
    """Compute accuracy in a region of thickness around the contours
        for binary images (0-1 values)
    Args:
        pred_im (Image): Prediction
        gt_im (Image): Target
        thickness (int, optional): [description]. Defaults to 8.
    """
    W, H = gt_im.size
    contours, hierarchy = cv2.findContours(
        np.array(gt_im), mode=cv2.RETR_TREE, method=cv2.CHAIN_APPROX_SIMPLE
    )
    mask_contour = np.zeros((H, W), dtype=np.int32)
    cv2.drawContours(
        mask_contour, contours, -1, (1), thickness=thickness, hierarchy=hierarchy
    )
    gt_contour = np.array(gt_im)[np.where(mask_contour > 0)]
    pred_contour = np.array(pred_im)[np.where(mask_contour > 0)]
    return get_accuracy(pred_contour, gt_contour)


def iou(pred_im, gt_im):
    """
    IoU for binary masks (0-1 values)

    Args:
        pred_im ([type]): [description]
        gt_im ([type]): [description]
    """
    pred = np.array(pred_im)
    gt = np.array(gt_im)
    intersection = (pred * gt).sum()
    union = (pred + gt).sum() - intersection
    return intersection / union


def f1_score(pred_im, gt_im):
    pred = np.array(pred_im)
    gt = np.array(gt_im)
    intersection = (pred * gt).sum()
    return 2 * intersection / (pred + gt).sum()


def accuracy(pred_im, gt_im):
    pred = np.array(pred_im)
    gt = np.array(gt_im)
    if len(gt_im.shape) == 4:
        assert gt_im.shape[1] == 1
        gt_im = gt_im[:, 0, :, :]
    if len(pred.shape) > len(gt_im.shape):
        pred = np.argmax(pred, axis=1)
    return float((pred == gt).sum()) / gt.size


def mIOU(pred, label, average="macro"):
    """
    Adapted from:
    https://stackoverflow.com/questions/62461379/multiclass-semantic-segmentation-model-evaluation

    Compute the mean IOU from pred and label tensors
    pred is a tensor N x C x H x W with logits (softmax will be applied)
    and label is a N x H  x W tensor with int labels per pixel

    this does the same as sklearn's jaccard_score function if you choose average="macro"
    Args:
        pred (torch.tensor): predicted logits
        label (torch.tensor): labels
        average: "macro" or "weighted"

    Returns:
        float: mIOU, can be nan
    """
    num_classes = pred.shape[-3]

    pred = torch.argmax(pred, dim=1).squeeze(1)
    present_iou_list = list()
    pred = pred.view(-1)
    label = label.view(-1)
    # Note: Following for loop goes from 0 to (num_classes-1)
    # and ignore_index is num_classes, thus ignore_index is
    # not considered in computation of IoU.
    interesting_classes = (
        [*range(num_classes)] if num_classes > 2 else [int(label.max().item())]
    )
    weights = []

    for sem_class in interesting_classes:
        pred_inds = pred == sem_class
        target_inds = label == sem_class
        if (target_inds.long().sum().item() > 0) or (pred_inds.long().sum().item() > 0):
            intersection_now = (pred_inds[target_inds]).long().sum().item()
            union_now = (
                pred_inds.long().sum().item()
                + target_inds.long().sum().item()
                - intersection_now
            )
            weights.append(pred_inds.long().sum().item())
            iou_now = float(intersection_now) / float(union_now)
            present_iou_list.append(iou_now)
    if not present_iou_list:
        return float("nan")
    elif average == "weighted":
        weighted_avg = np.sum(np.multiply(weights, present_iou_list) / np.sum(weights))
        return weighted_avg
    else:
        return np.mean(present_iou_list)
