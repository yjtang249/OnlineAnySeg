import os
import argparse
import numpy as np
import torch
import open3d as o3d
from urllib.request import urlretrieve
from util.visualization import get_colored_point_cloud_feature
from util.misc import extract_features

from model.resunet import ResUNetBN2C


"""给定一个input pointcloud, 提取per-point feature, 并将pointcloud用TSNE可视化(feature相似度高的点颜色相似)"""


# if not os.path.isfile('model/ResUNetBN2C-16feat-3conv.pth'):
#     print('Downloading weights...')
#     urlretrieve("https://node1.chrischoy.org/data/publications/fcgf/2019-09-18_14-15-59.pth", 'ResUNetBN2C-16feat-3conv.pth')
#
# if not os.path.isfile('test/redkitchen-20.ply'):
#     print('Downloading a mesh...')
#     urlretrieve("https://node1.chrischoy.org/data/publications/fcgf/redkitchen-20.ply", 'redkitchen-20.ply')


def demo(config):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    checkpoint = torch.load(config.model)
    model = ResUNetBN2C(1, 16, normalize_feature=True, conv1_kernel_size=3, D=3)
    model.load_state_dict(checkpoint['state_dict'])
    model.eval()

    model = model.to(device)

    pcd = o3d.io.read_point_cloud(config.input)
    xyz_down, feature = extract_features(model, xyz=np.array(pcd.points), voxel_size=config.voxel_size, device=device, skip_check=True)  # ndarray(ds_pts_num, 3) / ndarray(ds_pts_num, feature_dim)

    vis_pcd = o3d.geometry.PointCloud()
    vis_pcd.points = o3d.utility.Vector3dVector(xyz_down)

    vis_pcd = get_colored_point_cloud_feature(vis_pcd, feature.detach().cpu().numpy(), config.voxel_size)
    o3d.visualization.draw_geometries([vis_pcd])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-i',
        '--input',
        default='redkitchen-20.ply',
        type=str,
        help='path to a pointcloud file')
    parser.add_argument(
        '-m',
        '--model',
        default='model/ResUNetBN2C-16feat-3conv.pth',
        type=str,
        help='path to latest checkpoint (default: None)')
    parser.add_argument(
        '--voxel_size',
        default=0.025,
        type=float,
        help='voxel size to preprocess point cloud')

    config = parser.parse_args()
    demo(config)
