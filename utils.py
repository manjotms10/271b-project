import glob
import shutil

import numpy as np
import os

import torch
from matplotlib import pyplot as plt
from PIL import Image

cmap = plt.cm.jet

def get_output_directory(args):
    save_dir_root = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    save_dir_root = os.path.join(save_dir_root, 'result', args.dataset)
    if args.resume:
        runs = sorted(glob.glob(os.path.join(save_dir_root, 'run_*')))
        run_id = int(runs[-1].split('_')[-1]) if runs else 0
    else:
        runs = sorted(glob.glob(os.path.join(save_dir_root, 'run_*')))
        run_id = int(runs[-1].split('_')[-1]) + 1 if runs else 0
    save_dir = os.path.join(save_dir_root, 'run_' + str(run_id))
    return save_dir


"""
After obtaining ordinal labels for each position of Image,
the predicted depth value d(w, h) can be decoded as below.
"""


def get_depth_sid(args, labels, device):
    if args.dataset == 'kitti':
        min = 0.001
        max = 80.0
        K = 71.0
    elif args.dataset == 'nyu':
        min = 0.02
        max = 80.0
        K = 68.0
    else:
        print('No Dataset named as ', args.dataset)

    alpha_ = torch.Tensor(min).to(device)
    beta_ = torch.Tensor(max).to(device)
    K_ = torch.Tensor(K).to(device)

    depth = torch.exp(torch.log(alpha_) + torch.log(beta_ / alpha_) * labels / K_)
    return depth.float()


def get_labels_sid(args, depth, device):
    if args.dataset == 'kitti':
        alpha = 0.001
        beta = 80.0
        K = 71.0
    elif args.dataset == 'nyu':
        alpha = 0.02
        beta = 10.0
        K = 68.0
    else:
        print('No Dataset named as ', args.dataset)

    alpha = torch.Tensor(alpha).to(device)
    beta = torch.Tensor(beta).to(device)
    K = torch.Tensor(K).to(device)

    labels = K * torch.log(depth / alpha) / torch.log(beta / alpha)

    labels = labels.to(device)
    return labels.int()


# save checkpoint
def save_checkpoint(state, is_best, epoch, output_directory):
    checkpoint_filename = os.path.join(output_directory, 'checkpoint-' + str(epoch) + '.pth.tar')
    torch.save(state, checkpoint_filename)
    if is_best:
        best_filename = os.path.join(output_directory, 'model_best.pth.tar')
        shutil.copyfile(checkpoint_filename, best_filename)


def colored_depthmap(depth, d_min=None, d_max=None):
    if d_min is None:
        d_min = np.min(depth)
    if d_max is None:
        d_max = np.max(depth)
    depth_relative = (depth - d_min) / (d_max - d_min)
    return 255 * cmap(depth_relative)[:, :, :3]  # H, W, C


def merge_into_row(input, depth_target, depth_pred):
    rgb = 255 * np.transpose(np.squeeze(input.cpu().numpy()), (1, 2, 0))  # H, W, C
    depth_target_cpu = np.squeeze(depth_target.cpu().numpy())
    depth_pred_cpu = np.squeeze(depth_pred.data.cpu().numpy())

    d_min = min(np.min(depth_target_cpu), np.min(depth_pred_cpu))
    d_max = max(np.max(depth_target_cpu), np.max(depth_pred_cpu))
    depth_target_col = colored_depthmap(depth_target_cpu, d_min, d_max)
    depth_pred_col = colored_depthmap(depth_pred_cpu, d_min, d_max)
    img_merge = np.hstack([rgb, depth_target_col, depth_pred_col])

    return img_merge


def merge_into_row_with_gt(input, depth_input, depth_target, depth_pred):
    rgb = 255 * np.transpose(np.squeeze(input.cpu().numpy()), (1, 2, 0))  # H, W, C
    depth_input_cpu = np.squeeze(depth_input.cpu().numpy())
    depth_target_cpu = np.squeeze(depth_target.cpu().numpy())
    depth_pred_cpu = np.squeeze(depth_pred.data.cpu().numpy())

    d_min = min(np.min(depth_input_cpu), np.min(depth_target_cpu), np.min(depth_pred_cpu))
    d_max = max(np.max(depth_input_cpu), np.max(depth_target_cpu), np.max(depth_pred_cpu))
    depth_input_col = colored_depthmap(depth_input_cpu, d_min, d_max)
    depth_target_col = colored_depthmap(depth_target_cpu, d_min, d_max)
    depth_pred_col = colored_depthmap(depth_pred_cpu, d_min, d_max)

    img_merge = np.hstack([rgb, depth_input_col, depth_target_col, depth_pred_col])

    return img_merge


def add_row(img_merge, row):
    return np.vstack([img_merge, row])


def save_image(img_merge, filename):
    img_merge = Image.fromarray(img_merge.astype('uint8'))
    img_merge.save(filename)