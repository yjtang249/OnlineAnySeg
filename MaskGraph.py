import numpy as np
import torch
import math

from Instance import Instance
from tool.geometric_helpers import compute_intersection, add_wo_redundant, compute_complementary_w_mask
from tool.helper_functions import query_values_from_keys
from tool.visualization_helpers import generate_distinct_colors


class MaskGraph:
    def __init__(self, cfg, dataset, voxel_grids, device="cuda:0"):
        self.cfg = cfg
        self.dataset = dataset
        self.device = device

        self.voxel_grids = voxel_grids
        self.voxel_hash_table = self.voxel_grids.voxel_hash_table

        # vars to record information seg frame
        self.consider_bound_pts = True
        self.t_mask_num = self.cfg["scene"]["mask_num"]  # total mask num
        self.seg_interval = self.cfg["seg"]["seg_add_interval"]  # num of segment frame
        self.seg_frame_num = math.ceil(self.dataset.__len__() // self.seg_interval) + 1
        self.mask_visible_threshold = self.cfg["mask"]["mask_visible_threshold"  ]  # default: 0.1
        self.mask_visible_min_overlap = self.cfg["mask"]["mask_visible_min_overlap"]  # default: 500
        self.contain_min_threshold = self.cfg["mask"]["contain_min_threshold"]  # default: 0.3
        self.max_voxel_num = self.voxel_hash_table.max_voxel_num
        self.f_mask_voxels_clouds = {}  # a dict, key is mask's f_mask_id, (frame_id, mask_id); value is mask's corresponding voxel coordinates(in World CS), Tensor(m_pts_num, ), dtype=int64;
        self.mask_voxels_coords = {}  # a dict, key is merged mask's merged_mask_id(1D); value merged mask's corresponding voxel_ids, Tensor(m_pts_num, ), dtype=int64;

        self.contain_matrix = torch.zeros((self.t_mask_num, self.t_mask_num), dtype=torch.float32, device=self.device)

        # *** mask_id of each voxel on each seg_frame, Tensor(max_voxel_num, seg_frame_num)
        self.voxel_in_frame_matrix = torch.zeros((self.max_voxel_num, self.seg_frame_num), dtype=torch.float32, device=self.device)  # fill-only (never be modified)

        self.original_mask_in_frame_mat = []  #  seg_frame_ID for each original mask (append only)
        self.merged_mask_in_frame_mat = torch.zeros((self.t_mask_num, self.seg_frame_num), dtype=torch.float32, device=self.device)  # each merged mask's corresponding 的seg frame
        self.boundary_voxel_indices = None

        # Instance dict
        self.t_instance_num = 0
        self.color_bank = generate_distinct_colors(10000)  # RGB list, each RGB value belongs to [0, 1], Tensor(t_mask_num, 3), dtype=float32
        self.instance_dict = {}


    @property
    def get_original_mask_in_frame_mat(self):
        return torch.concat(self.original_mask_in_frame_mat,  dim=0)

    @property
    def cur_instance_num(self):
        return len(self.instance_dict)

    def get_instance_rgb(self, instance_ids, norm_to_1=False):
        cur_inst_num = self.cur_instance_num
        if norm_to_1:
            rgb_list = [self.instance_dict[i].rgb for i in range(cur_inst_num) if i in instance_ids]
        else:
            rgb_list = [255. * self.instance_dict[i].rgb for i in range(cur_inst_num) if i in instance_ids]
        return rgb_list

    def expand_vars(self):
        # TODO
        print(3)


    # @brief: update boundary voxels of the scene;
    # @param frame_boundary_voxels_indices:
    # @param sort_flag: whether to sort all boundary voxels by their indices, bool.
    def update_boundary_voxels(self, frame_boundary_voxels_indices, sort_flag=True):
        if frame_boundary_voxels_indices is None:
            return

        if self.boundary_voxel_indices is None:
            self.boundary_voxel_indices = frame_boundary_voxels_indices
        else:
            self.boundary_voxel_indices = add_wo_redundant(self.boundary_voxel_indices, frame_boundary_voxels_indices)

        if sort_flag:
            self.boundary_voxel_indices = torch.sort(self.boundary_voxel_indices)[0]


    # @brief: for a set of original 2D masks in a new frame, record each mask's corresponding voxels (also processing boundary points)
    def add_frame_masks(self, frame_id, mask_dict):
        seg_frame_id = frame_id // self.seg_interval
        appeared_voxel_indices = None
        frame_boundary_voxels_indices = None
        mask_in_frame_list = []

        # Step 1: record each mask's corresponding voxels
        for mask_id, mask_voxel_coords in mask_dict.items():
            # Step 1.1: record this mask's corresponding voxels
            mask_voxel_indices = self.voxel_hash_table.voxel_coords2voxel_indices(mask_voxel_coords)
            self.voxel_in_frame_matrix[mask_voxel_indices, seg_frame_id] = mask_id  # *** 填充self.voxel_in_frame_matrix

            # Step 1.2: record this mask's seg_frame_ID
            mask_in_frame = torch.zeros((self.seg_frame_num, ), dtype=torch.float32, device=self.device)
            mask_in_frame[seg_frame_id] = 1
            self.original_mask_in_frame_mat.append(mask_in_frame)
            mask_in_frame_list.append(mask_in_frame)

            # Step 1.3: update boundary voxels in this frame (if a voxel belongs to >= 2 masks, this voxel is boundary voxel)
            if appeared_voxel_indices is not None:
                new_frame_boundary_voxels_indices = compute_intersection(mask_voxel_indices, appeared_voxel_indices)
                frame_boundary_voxels_indices = add_wo_redundant(new_frame_boundary_voxels_indices, frame_boundary_voxels_indices)

            # Step 1.3: record appeared voxels in this frame so far
            appeared_voxel_indices = add_wo_redundant(mask_voxel_indices, appeared_voxel_indices)

        if self.consider_bound_pts:
            # Step 2: for each mask, subtract boundary voxels from its original voxel set
            # self.update_boundary_voxels(frame_boundary_voxels_indices)
            new_mask_voxel_dict, new_mask_indices_dict = self.get_mask_dict_wo_bound(mask_dict, frame_boundary_voxels_indices)
        else:
            new_mask_voxel_dict, new_mask_indices_dict = self.get_mask_dict_wo_bound(mask_dict, None)

        return new_mask_voxel_dict, new_mask_indices_dict, mask_in_frame_list


    # @brief: given some masks' voxel coordinates and boundary voxels, get each mask's non-boundary voxel coordinates
    def get_mask_dict_wo_bound(self, mask_dict, boundary_voxels_indices=None):
        new_mask_voxel_dict = {}
        new_mask_indices_dict = {}
        for mask_id, mask_voxel_coords in mask_dict.items():
            mask_voxel_indices = self.voxel_hash_table.voxel_coords2voxel_indices(mask_voxel_coords)

            if boundary_voxels_indices is not None:
                _, valid_indices_this = compute_complementary_w_mask(mask_voxel_indices, boundary_voxels_indices)
                new_mask_voxel_dict[mask_id] = mask_voxel_coords[valid_indices_this]
                new_mask_indices_dict[mask_id] = mask_voxel_indices[valid_indices_this]
            else:
                new_mask_voxel_dict[mask_id] = mask_voxel_coords
                new_mask_indices_dict[mask_id] = mask_voxel_indices
        return new_mask_voxel_dict, new_mask_indices_dict


    # @brief: create a new Instance obj for an original/merged mask, and insert it into Instance Dict
    def insert_instance(self, frame_id, mask_id, merged_mask_id, mask_voxel_coords, mask_voxel_indices, mask_sem_feature):
        ori_mask_list = [(frame_id, mask_id)]
        rgb_this = self.color_bank[self.t_instance_num]

        # create a new Instance obj
        inst = Instance(frame_id, merged_mask_id, mask_voxel_coords, mask_voxel_indices, ori_mask_list, mask_sem_feature, self.cfg, rgb_this, self.device)

        self.instance_dict[merged_mask_id] = inst
        self.t_instance_num += 1


    # @brief: for all merged_maks, merge some of them and keep the rest
    # @param masks_to_keep: list of int;
    # @param mask_clusters: list of list / None;
    # @param c_mask_num_before: int;
    #-@return new_mask_counter: number of new masks, int.
    def keep_and_merge_masks(self, frame_id, c_mask_num_before, masks_to_keep, mask_clusters=None):
        instance_dict_new = {}
        merged_mask_in_frame_mat_new = torch.zeros((c_mask_num_before, self.seg_frame_num), dtype=torch.float32, device=self.device)  # visible voxel number that each merged mask on seg frames

        # Step 1: for each kept mask
        new_mask_counter = 0
        for mask_id_kept in masks_to_keep:
            instance_dict_new[new_mask_counter] = self.instance_dict[mask_id_kept]
            merged_mask_in_frame_mat_new[new_mask_counter] = self.merged_mask_in_frame_mat[mask_id_kept]
            new_mask_counter += 1

        # Step 2: for each mask cluster to merge
        if mask_clusters is not None:
            for mask_cluster in mask_clusters:
                instance_list = [self.instance_dict[i] for i in mask_cluster]
                new_instance = Instance.creat_instance_from_list(frame_id, new_mask_counter, instance_list, self.cfg, self.device)
                instance_dict_new[new_mask_counter] = new_instance

                merged_mask_in_frame_row = self.merged_mask_in_frame_mat[mask_cluster].sum(dim=0).bool().float()
                merged_mask_in_frame_mat_new[new_mask_counter] = merged_mask_in_frame_row
                new_mask_counter += 1

        self.instance_dict = instance_dict_new
        self.merged_mask_in_frame_mat[:c_mask_num_before] = merged_mask_in_frame_mat_new
        return new_mask_counter


    # @brief: giving a quert mask (n 3D coordinates), find all masks that have overlap with it, and compute the overlap voxel number with each mask,
    #         as well as the mutual containing ratio between each overlapping masks;
    # @param mask_frame: this mask's corresponding seg frame, 0/1, Tensor(seg_frame_num, );
    # @param voxel_coords: Voxel coordinates(in World Coordinate System) of query mask, Tensor(n, 3), dtype=float32;
    # @param id_mapping: mapping global_mask_id to merged_mask_id, list of int;
    # @param valid_merge_mask_ids: merge_mask_ids to count, list/None;
    #-@return overlap_mask_ids: mask_IDs that has overlap with current input voxel set, Tensor(o_mask_num, );
    #-@return contain_ratio_q2o_final: containig ratio -- query mask to each overlap mask, Tensor(o_mask_num, );
    #-@return contain_ratio_o2q_final: containig ratio -- each overlap mask to query mask, Tensor(o_mask_num, ).
    def query_mask_under_visible_part(self, mask_frame, voxel_coords, id_mapping, valid_merge_mask_ids=None):
        if -1 in id_mapping:
            id_mapping_valid = [id for id in id_mapping if id != -1]
            valid_merge_mask_ids = list( set(id_mapping_valid) )

        # Step 1: query hash table, to get input voxels' binding mask info; then for each overlap mask, count its overlap voxel number
        overlap_mask_ids, overlap_mask_counts, voxel_ids = self.voxel_hash_table.query_mask_w_mapping(voxel_coords, id_mapping, valid_merge_mask_ids)
        if overlap_mask_ids.shape[0] == 0:
            return overlap_mask_ids, None, None

        # Step 2: (q_in_o_count) for each overlap mask, compute query mask's visible voxel num on its corrsponding frame set
        mask_voxel_frame = self.voxel_in_frame_matrix[voxel_ids].clamp(max=1.)  # Tensor(mask_voxel_num, seg_frame_num)
        o_mask_frame = self.merged_mask_in_frame_mat[overlap_mask_ids]  # Tensor(o_mask_num, seg_frame_num)
        mask_voxel_o_mask = (mask_voxel_frame @ o_mask_frame.T).clamp(max=1.)  # whether each voxel in this mask can be seen by each overlap_mask, Tensor(mask_voxel_num, o_mask_num)
        q_in_o_vis_count = torch.sum(mask_voxel_o_mask, dim=0)  # Tensor(o_mask_num, )
        q_in_o_vis_ratio = q_in_o_vis_count / voxel_coords.shape[0]  # query mask's visible ratio by each overlap mask, Tensor(o_mask_num, )

        # Step 3: (o_in_q_count) for each overlap mask, compute its visible size on query mask's frame set
        o_in_q_vis_count = torch.zeros_like(q_in_o_vis_count)
        o_in_q_vis_ratio = torch.zeros_like(q_in_o_vis_ratio)
        overlap_mask_size = torch.zeros_like(o_in_q_vis_count)  # mask size of each (merged) overlap mask
        for i, o_mask_id in enumerate(overlap_mask_ids):
            o_voxel_indices = self.instance_dict[o_mask_id.item()].mask_voxel_indices
            o_mask_voxel_frame = self.voxel_in_frame_matrix[o_voxel_indices].clamp(max=1)  # Tensor(o_mask_voxel_num, seg_frame_num)
            o_mask_voxel_frame = mask_frame.unsqueeze(0) * o_mask_voxel_frame  # only consider query mask's frame set, Tensor(o_mask_voxel_num, seg_frame_num)
            o_in_q_count_i = o_mask_voxel_frame.any(-1).sum()
            o_in_q_vis_count[i] = o_in_q_count_i
            o_in_q_vis_ratio[i] = o_in_q_count_i / o_voxel_indices.shape[0]
            overlap_mask_size[i] = o_voxel_indices.shape[0]

        # Step 4: count containing ratio between query mask and each overlap mask
        contain_ratio_q2o = overlap_mask_counts.float() / o_in_q_vis_count  # the ratio that query mask contains overlap masks
        q_in_o_condition = (contain_ratio_q2o > self.contain_min_threshold) & torch.logical_or(o_in_q_vis_ratio > self.mask_visible_threshold , o_in_q_vis_count > self.mask_visible_min_overlap)
        contain_ratio_q2o_final = torch.where(q_in_o_condition, contain_ratio_q2o, torch.zeros_like(contain_ratio_q2o))
        contain_ratio_q2o_final = contain_ratio_q2o_final.clamp(max=1.)

        contain_ratio_o2q = overlap_mask_counts.float() / q_in_o_vis_count  # the ratio that overlap masks contain query mask
        o_in_q_condition = (contain_ratio_o2q > self.contain_min_threshold) & torch.logical_or(q_in_o_vis_ratio > self.mask_visible_threshold, q_in_o_vis_count > self.mask_visible_min_overlap)
        contain_ratio_o2q_final = torch.where(o_in_q_condition, contain_ratio_o2q, torch.zeros_like(contain_ratio_o2q))
        contain_ratio_o2q_final = contain_ratio_o2q_final.clamp(max=1.)

        return overlap_mask_ids, contain_ratio_q2o_final, contain_ratio_o2q_final


    # @brief: re-compute mutual containing ratio between given merge masks;
    # @param merged_mask_ids: Tensor(a, )/list;
    #-@return: containing ratio mat --- [i, j] indicates the ratio that mask_i contains mask_j, Tensor(a, a).
    def compute_masks_contain_ratio(self, merged_mask_ids, id_mapping):
        if isinstance(merged_mask_ids, torch.Tensor):
            merged_mask_ids = merged_mask_ids.cpu().numpy().tolist()
        selected_mask_num = len(merged_mask_ids)
        if selected_mask_num == 0:
            return torch.eye(selected_mask_num, dtype=torch.float32, device=self.device)

        selected_mask_voxels = [self.instance_dict[mask_id].mask_voxel_coords for mask_id in merged_mask_ids]  # voxel coords list of selected merged masks
        selected_mask_voxel_indices = [self.instance_dict[mask_id].mask_voxel_indices for mask_id in merged_mask_ids]  # voxel indices list of selected masks

        # Step 1: compute visible voxel numbers between input merged masks
        all_masks_frame = self.merged_mask_in_frame_mat[merged_mask_ids]  # each input mask's corresponding seg frame, Tensor(a, seg_frame_num)
        i_in_a_vis_count_list = []  # [i, j]: the visible voxel number that mask_i is projected to mask_j's frame set, list of Tensor(a, )
        i_in_a_vis_ratio_list = []  # [i, j]: the visiblity ratio that mask_i is projected to mask_j's frame set, list of Tensor(a, )
        mask_id2index = {}  # mask_id-index mapping dict
        vis_count_thresh_mat = torch.zeros((selected_mask_num, selected_mask_num), dtype=torch.float32, device=self.device)

        for i, mask_id in enumerate(merged_mask_ids):
            mask_id2index[mask_id] = i
            voxel_ids_i = selected_mask_voxel_indices[i]
            mask_i_size = voxel_ids_i.shape[0]

            # project 3D mask_i to each mask's frame set
            mask_voxel_frame = self.voxel_in_frame_matrix[voxel_ids_i].clamp(max=1.)  # seg_frames corresponding to mask_i's voxels, Tensor(mask_voxel_num, seg_frame_num)
            mask_voxel_i_mask = (mask_voxel_frame @ all_masks_frame.T).clamp(max=1.)  # whether each voxel in this mask can be seen by each input mask, Tensor(mask_voxel_num, a)
            i_in_a_vis_count = torch.sum(mask_voxel_i_mask, dim=0)  # visible size of current mask(mask_i) on each input mask's frame set, Tensor(a, )
            i_in_a_vis_count_list.append(i_in_a_vis_count)
            i_in_a_vis_ratio_list.append(i_in_a_vis_count / mask_i_size)
            vis_count_thresh_mat[i, :] = mask_i_size * self.mask_visible_threshold

        vis_count_mat = torch.stack(i_in_a_vis_count_list, dim=0)  # [i, j]: the visible voxel number that mask_i is projected to mask_j's frame set, Tensor(a, a)
        vis_ratio_mat = torch.stack(i_in_a_vis_ratio_list, dim=0)  # [i, j]: the visiblity ratio that mask_i is projected to mask_j's frame set, Tensor(a, a)

        # Step 2: for each input mask, compute its overlap masks and corresponding overlap voxel numbers
        overlap_counts_list = []
        for mask_id, mask_voxel_coords in zip(merged_mask_ids, selected_mask_voxels):
            # query hash table, find overlap masks; and for each overlap mask, count its overlap voxel number
            overlap_mask_ids, overlap_mask_counts, voxel_ids = self.voxel_hash_table.query_mask_w_mapping(mask_voxel_coords, id_mapping, merged_mask_ids)

            overlap_counts_i = torch.zeros((selected_mask_num, ), dtype=torch.float32, device=self.device)  # the overlap voxel number between mask_i and each input mask, Tensor(a, )
            if overlap_mask_ids.shape[0] > 0:
                overlap_mask_indices = query_values_from_keys(mask_id2index, overlap_mask_ids, self.device)
                overlap_counts_i[overlap_mask_indices] = overlap_mask_counts.float()
            overlap_counts_list.append(overlap_counts_i)

        overlap_count_mat = torch.stack(overlap_counts_list, dim=0)  # [i, j]: the overlap voxel number between i-th mask and j-th mask, Tensor(a, a)

        # Step 3: compute contain ratio mat
        contained_ratio_mat = overlap_count_mat / vis_count_mat  # [i, j]: the ratio that i-th mask is contained by j-th mask, Tensor(a, a)

        contain_ratio_condition = torch.logical_or(vis_ratio_mat > self.mask_visible_threshold, vis_count_mat > self.mask_visible_min_overlap) & (contained_ratio_mat > self.contain_min_threshold)
        contained_ratio_mat_valid = torch.where(contain_ratio_condition, contained_ratio_mat, torch.zeros_like(contained_ratio_mat))
        contain_ratio_mat = contained_ratio_mat_valid.T
        return contain_ratio_mat
