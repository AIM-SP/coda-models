import copy
import pickle

import numpy as np
from skimage import io

from ...ops.roiaware_pool3d import roiaware_pool3d_utils
from ...utils import box_utils, calibration_kitti, common_utils, object3d_kitti, self_training_utils
from ..dataset import DatasetTemplate


class CODataset(DatasetTemplate):
    def __init__(self, dataset_cfg, class_names, training=True, root_path=None, logger=None, ps_label_dir=None, use_sorted_imageset=False):
        """
        Args:
            root_path:
            dataset_cfg:
            class_names:
            training:
            logger:
        """
        super().__init__(
            dataset_cfg=dataset_cfg, class_names=class_names, training=training, root_path=root_path, logger=logger, 
            ps_label_dir=ps_label_dir
        )

        self.use_sorted_imageset=use_sorted_imageset
        self.split = self.dataset_cfg.DATA_SPLIT[self.mode]
        self.root_split_path = self.root_path / ('training' if self.split != 'test' else 'testing')

        # Set sample idx split from imagesets, sorts if using demo
        self.set_sample_id_list(self.split)

        self.coda_infos = []
        self.include_coda_data(self.mode)

        if self.training and self.dataset_cfg.get('BALANCED_RESAMPLING', False):
            self.coda_infos = self.balanced_infos_resampling(self.coda_infos)

        # Build idx to imageset idx map
        if self.use_sorted_imageset:
            self.build_idx_to_imageset_map()

    def include_coda_data(self, mode):
        """
        Assumes imageset is generated before running create_coda_dataset. Important for maintaing that
        order of self.coda_infos matches assigned imageset idx
        """
        if self.logger is not None:
            self.logger.info('Loading CODa dataset')
        coda_infos = []

        for info_path in self.dataset_cfg.INFO_PATH[mode]:
            info_path = self.root_path / info_path
            if not info_path.exists():
                continue
            with open(info_path, 'rb') as f:
                infos = pickle.load(f)
                coda_infos.extend(infos)

        self.coda_infos.extend(coda_infos)
        if self.logger is not None:
            self.logger.info('Total samples for CODa dataset: %d' % (len(self.coda_infos)))
    
    def balanced_infos_resampling(self, infos):
        """
        Class-balanced sampling of CODa dataset from https://arxiv.org/abs/1908.09492
        """
        if self.class_names is None:
            return infos

        cls_infos = {name: [] for name in self.class_names}

        for info in infos:
            for name in set(info['annos']['name']):
                if name in self.class_names:
                    cls_infos[name].append(info)

        duplicated_samples = sum([len(v) for _, v in cls_infos.items()])
        cls_dist = {k: len(v) / duplicated_samples for k, v in cls_infos.items()}

        sampled_infos = []

        frac = 1.0 / len(self.class_names)
        ratios = [frac / (v+1e-3) for v in cls_dist.values()] # added 1e-3 to fix no class instance

        for cur_cls_infos, ratio in zip(list(cls_infos.values()), ratios):
            sampled_infos += np.random.choice(
                cur_cls_infos, int(len(cur_cls_infos) * ratio)
            ).tolist()
        self.logger.info('Total samples after balanced resampling: %s' % (len(sampled_infos)))

        cls_infos_new = {name: [] for name in self.class_names}
        for info in sampled_infos:
            for name in set(info['annos']['name']):
                if name in self.class_names:
                    cls_infos_new[name].append(info)

        cls_dist_new = {k: len(v) / len(sampled_infos) for k, v in cls_infos_new.items()}

        return sampled_infos

    def build_idx_to_imageset_map(self):
        """
        Generates array to idx from idx to imageset sample idx in order
        """
        #1 Iterate through all indices for all imagesets
        splits = ["train", "test", "val"]

        # Create conglomerate of all infos list
        self.lidar_idx_subdir_map     = {}
        all_coda_infos              = []
        infos_lidar_idx_list        = []
        for split in splits:
            for info_path in self.dataset_cfg.INFO_PATH[split]:
                info_path = self.root_path / info_path

                mode = self.dataset_cfg.DATA_SPLIT[split]
                with open(info_path, 'rb') as f:
                    infos = pickle.load(f)
                    print(f'Adding infos {len(infos)} for split {split} and mode {mode}...')
                    all_coda_infos.extend(infos)

                    for info_idx, info in enumerate(infos):
                        infos_lidar_idx_list.append(info['point_cloud']['lidar_idx'])
                        self.lidar_idx_subdir_map[info['point_cloud']['lidar_idx']] = \
                            'training' if mode != 'test' else 'testing' # 

        infos_lidar_idx_np = np.array([int(lidar_idx) for lidar_idx in infos_lidar_idx_list])

        # Use the following in getitem
        self.sorted_lidar_idx_map   = np.argsort(infos_lidar_idx_np)
        self.coda_infos = all_coda_infos

    def set_sample_id_list(self, split):
        split_dir = self.root_path / 'ImageSets' / (self.split + '.txt')
        self.sample_id_list = [x.strip() for x in open(split_dir).readlines()] if split_dir.exists() else None

    def set_split(self, split):
        super().__init__(
            dataset_cfg=self.dataset_cfg, class_names=self.class_names, training=self.training, root_path=self.root_path, logger=self.logger
        )
        self.split = split
        self.root_split_path = self.root_path / ('training' if self.split != 'test' else 'testing')
        self.set_sample_id_list(split)

    def get_lidar(self, idx):
        root_split_path = self.root_split_path
        if self.use_sorted_imageset:
            root_split_path = self.root_path / self.lidar_idx_subdir_map[idx]

        lidar_file = root_split_path / 'velodyne' / ('%s.bin' % idx)
        assert lidar_file.exists(), "Lidar files %s " % str(lidar_file)
        
        return np.fromfile(str(lidar_file), dtype=np.float32).reshape(-1, 4)

    def get_image_shape(self, idx):
        root_split_path = self.root_split_path
        if self.use_sorted_imageset:
            root_split_path = self.root_path / self.lidar_idx_subdir_map[idx]

        img_file = root_split_path / 'image_0' / ('%s.jpg' % idx)
        assert img_file.exists(), "Image file %s does not exist" % img_file
        return np.array(io.imread(img_file).shape[:2], dtype=np.int32)

    def get_label(self, idx):
        root_split_path = self.root_split_path
        if self.use_sorted_imageset:
            root_split_path = self.root_path / self.lidar_idx_subdir_map[idx]

        label_file = root_split_path / 'label_0' / ('%s.txt' % idx)
        assert label_file.exists(), "Label file %s does not exist" % label_file
        return object3d_kitti.get_objects_from_label(label_file)

    def get_calib(self, idx):
        root_split_path = self.root_split_path
        if self.use_sorted_imageset:
            root_split_path = self.root_path / self.lidar_idx_subdir_map[idx]
        
        calib_file = root_split_path / 'calib' / ('%s.txt' % idx)
        assert calib_file.exists()
        return calibration_kitti.Calibration(calib_file, use_coda=True)

    def get_road_plane(self, idx):
        root_split_path = self.root_split_path
        if self.use_sorted_imageset:
            root_split_path = self.root_path / self.lidar_idx_subdir_map[idx]

        plane_file = root_split_path / 'planes' / ('%s.txt' % idx)
        if not plane_file.exists():
            return None

        with open(plane_file, 'r') as f:
            lines = f.readlines()
        lines = [float(i) for i in lines[3].split()]
        plane = np.asarray(lines)

        # Ensure normal is always facing up, this is in the rectified camera coordinate
        if plane[1] > 0:
            plane = -plane

        norm = np.linalg.norm(plane[0:3])
        plane = plane / norm
        return plane

    @staticmethod
    def get_fov_flag(pts_rect, img_shape, calib, margin=0):
        """
        Args:
            pts_rect:
            img_shape:
            calib:
            margin
        Returns:

        """
        pts_img, pts_rect_depth = calib.rect_to_img(pts_rect)
        val_flag_1 = np.logical_and(pts_img[:, 0] >= 0 - margin, pts_img[:, 0] < img_shape[1] + margin)
        val_flag_2 = np.logical_and(pts_img[:, 1] >= 0 - margin, pts_img[:, 1] < img_shape[0] + margin)
        val_flag_merge = np.logical_and(val_flag_1, val_flag_2)
        pts_valid_flag = np.logical_and(val_flag_merge, pts_rect_depth >= 0)

        return pts_valid_flag

    def get_infos(self, num_workers=4, has_label=True, count_inside_pts=True, sample_id_list=None):
        import concurrent.futures as futures

        def process_single_scene(sample_idx):
            print('%s sample_idx: %s' % (self.split, sample_idx))
            info = {}
            pc_info = {'num_features': 4, 'lidar_idx': sample_idx}
            info['point_cloud'] = pc_info

            image_info = {'image_idx': sample_idx, 'image_shape': self.get_image_shape(sample_idx)}
            info['image'] = image_info
            calib = self.get_calib(sample_idx)

            P2 = np.concatenate([calib.P2, np.array([[0., 0., 0., 1.]])], axis=0)
            R0_4x4 = np.zeros([4, 4], dtype=calib.R0.dtype)
            R0_4x4[3, 3] = 1.
            R0_4x4[:3, :3] = calib.R0
            V2C_4x4 = np.concatenate([calib.V2C, np.array([[0., 0., 0., 1.]])], axis=0)
            # Store calib info as P2 key for easier downstream processing
            calib_info = {'P2': P2, 'R0_rect': R0_4x4, 'Tr_velo_to_cam': V2C_4x4}

            info['calib'] = calib_info

            if has_label:
                obj_list = self.get_label(sample_idx)
                annotations = {}
                if len(obj_list)>0:
                    annotations['name'] = np.array([obj.cls_type for obj in obj_list])
                    annotations['truncated'] = np.array([obj.truncation for obj in obj_list])
                    annotations['occluded'] = np.array([obj.occlusion for obj in obj_list])
                    annotations['alpha'] = np.array([obj.alpha for obj in obj_list])
                    annotations['bbox'] = np.concatenate([obj.box2d.reshape(1, 4) for obj in obj_list], axis=0)
                    annotations['dimensions'] = np.array([[obj.l, obj.h, obj.w] for obj in obj_list])  # lhw(camera) format
                    annotations['location'] = np.concatenate([obj.loc.reshape(1, 3) for obj in obj_list], axis=0)
                    annotations['rotation_y'] = np.array([obj.ry for obj in obj_list])
                    annotations['score'] = np.array([obj.score for obj in obj_list])
                    annotations['difficulty'] = np.array([obj.level for obj in obj_list], np.int32)
                else:
                    annotations['name'] = np.array([])
                    annotations['truncated'] = np.array([])
                    annotations['occluded'] = np.array([])
                    annotations['alpha'] = np.array([])
                    annotations['bbox'] = np.empty((0, 4))
                    annotations['dimensions'] = np.empty((0,3)) # lhw(camera) format
                    annotations['location'] = np.empty((0,3))
                    annotations['rotation_y'] = np.array([])
                    annotations['score'] = np.array([])
                    annotations['difficulty'] = np.array([], np.int32)

                num_objects = len([obj.cls_type for obj in obj_list if obj.cls_type != 'DontCare'])
                num_gt = len(annotations['name'])
                index = np.argwhere( np.isin([obj.cls_type for obj in obj_list], ['DontCare'], invert=True) ).reshape(-1,)
                # list(range(num_objects)) + [-1] * (num_gt - num_objects)
                annotations['index'] = index.astype(np.int32) # np.array(index, dtype=np.int32) #TODO: refactor later
                
                loc = annotations['location'][index]
                dims = annotations['dimensions'][index]
                rots = annotations['rotation_y'][index]
                loc_lidar = calib.rect_to_lidar(loc)
                l, h, w = dims[:, 0:1], dims[:, 1:2], dims[:, 2:3]
                loc_lidar[:, 2] += h[:, 0] / 2
                gt_boxes_lidar = np.concatenate([loc_lidar, l, w, h, -(np.pi / 2 + rots[..., np.newaxis])], axis=1)
                annotations['gt_boxes_lidar'] = gt_boxes_lidar

                info['annos'] = annotations

                # Check that all other keys besides gt boxes lidar and index match
                prev_key = None
                for key in info['annos'].keys():
                    if key == 'index' or key=='gt_boxes_lidar':
                        continue
                    if prev_key == None:
                        prev_key = key
                    assert len(info['annos'][key]) == len(info['annos'][prev_key]), "Keys %s and %s do not \
                        match" % (key, prev_key)
                    prev_key = key

                if count_inside_pts:
                    points = self.get_lidar(sample_idx)
                    calib = self.get_calib(sample_idx)
                    pts_rect = calib.lidar_to_rect(points[:, 0:3])

                    fov_flag = self.get_fov_flag(pts_rect, info['image']['image_shape'], calib)
                    pts_fov = points[fov_flag]
                    corners_lidar = box_utils.boxes_to_corners_3d(gt_boxes_lidar)
                    num_points_in_gt = -np.ones(num_gt, dtype=np.int32)

                    for k in range(num_objects):
                        flag = box_utils.in_hull(pts_fov[:, 0:3], corners_lidar[k])
                        num_points_in_gt[k] = flag.sum()
                    annotations['num_points_in_gt'] = num_points_in_gt

            return info

        sample_id_list = sample_id_list if sample_id_list is not None else self.sample_id_list
        with futures.ThreadPoolExecutor(num_workers) as executor:
            infos = executor.map(process_single_scene, sample_id_list)
        return list(infos)

    def create_groundtruth_database(self, info_path=None, used_classes=None, split='train'):
        import torch

        database_save_path = Path(self.root_path) / ('gt_database' if split == 'train' else ('gt_database_%s' % split))
        db_info_save_path = Path(self.root_path) / ('coda_dbinfos_%s.pkl' % split)

        database_save_path.mkdir(parents=True, exist_ok=True)
        all_db_infos = {}

        with open(info_path, 'rb') as f:
            infos = pickle.load(f)

        for k in range(len(infos)):
            print('gt_database sample: %d/%d' % (k + 1, len(infos)))
            info = infos[k]
            sample_idx = info['point_cloud']['lidar_idx']
            points = self.get_lidar(sample_idx)
            annos = info['annos']
            names = annos['name']
            difficulty = annos['difficulty']
            bbox = annos['bbox']
            gt_boxes = annos['gt_boxes_lidar']

            num_obj = gt_boxes.shape[0]
            point_indices = roiaware_pool3d_utils.points_in_boxes_cpu(
                torch.from_numpy(points[:, 0:3]), torch.from_numpy(gt_boxes)
            ).numpy()  # (nboxes, npoints)

            for i in range(num_obj):
                filename = '%s_%s_%d.bin' % (sample_idx, names[i], i)
                filepath = database_save_path / filename
                gt_points = points[point_indices[i] > 0]

                gt_points[:, :3] -= gt_boxes[i, :3]
                with open(filepath, 'w') as f:
                    gt_points.tofile(f)

                if (used_classes is None) or names[i] in used_classes:
                    db_path = str(filepath.relative_to(self.root_path))  # gt_database/xxxxx.bin
                    db_info = {'name': names[i], 'path': db_path, 'image_idx': sample_idx, 'gt_idx': i,
                               'box3d_lidar': gt_boxes[i], 'num_points_in_gt': gt_points.shape[0],
                               'difficulty': difficulty[i], 'bbox': bbox[i], 'score': annos['score'][i]}
                    if names[i] in all_db_infos:
                        all_db_infos[names[i]].append(db_info)
                    else:
                        all_db_infos[names[i]] = [db_info]
        for k, v in all_db_infos.items():
            print('Database %s: %d' % (k, len(v)))

        with open(db_info_save_path, 'wb') as f:
            pickle.dump(all_db_infos, f)

    def generate_prediction_dicts(self, batch_dict, pred_dicts, class_names, output_path=None):
        """
        Args:
            batch_dict:
                frame_id:
            pred_dicts: list of pred_dicts
                pred_boxes: (N, 7), Tensor
                pred_scores: (N), Tensor
                pred_labels: (N), Tensor
            class_names:
            output_path:

        Returns:

        """
        def get_template_prediction(num_samples):
            ret_dict = {
                'name': np.zeros(num_samples), 'truncated': np.zeros(num_samples),
                'occluded': np.zeros(num_samples), 'alpha': np.zeros(num_samples),
                'bbox': np.zeros([num_samples, 4]), 'dimensions': np.zeros([num_samples, 3]),
                'location': np.zeros([num_samples, 3]), 'rotation_y': np.zeros(num_samples),
                'score': np.zeros(num_samples), 'boxes_lidar': np.zeros([num_samples, 7])
            }
            return ret_dict

        def generate_single_sample_dict(batch_index, box_dict):
            pred_scores = box_dict['pred_scores'].cpu().numpy()
            pred_boxes = box_dict['pred_boxes'].cpu().numpy()
            pred_labels = box_dict['pred_labels'].cpu().numpy()
            pred_dict = get_template_prediction(pred_scores.shape[0])
            if pred_scores.shape[0] == 0:
                return pred_dict

            calib = batch_dict['calib'][batch_index]
            image_shape = batch_dict['image_shape'][batch_index]

            if self.dataset_cfg.get('SHIFT_COOR', None):
                pred_boxes[:, 0:3] -= self.dataset_cfg.SHIFT_COOR

            # BOX FILTER
            if self.dataset_cfg.get('TEST', None) and self.dataset_cfg.TEST.BOX_FILTER['FOV_FILTER']:
                box_preds_lidar_center = pred_boxes[:, 0:3]
                pts_rect = calib.lidar_to_rect(box_preds_lidar_center)
                fov_flag = self.get_fov_flag(pts_rect, image_shape, calib, margin=5)
                pred_boxes = pred_boxes[fov_flag]
                pred_labels = pred_labels[fov_flag]
                pred_scores = pred_scores[fov_flag]

            pred_boxes_camera = box_utils.boxes3d_lidar_to_kitti_camera(pred_boxes, calib)
            pred_boxes_img = box_utils.boxes3d_kitti_camera_to_imageboxes(
                pred_boxes_camera, calib, image_shape=image_shape
            )

            pred_dict['name'] = np.array(class_names)[pred_labels - 1]
            pred_dict['alpha'] = -np.arctan2(-pred_boxes[:, 1], pred_boxes[:, 0]) + pred_boxes_camera[:, 6]
            pred_dict['bbox'] = pred_boxes_img
            pred_dict['dimensions'] = pred_boxes_camera[:, 3:6]
            pred_dict['location'] = pred_boxes_camera[:, 0:3]
            pred_dict['rotation_y'] = pred_boxes_camera[:, 6]
            pred_dict['score'] = pred_scores
            pred_dict['boxes_lidar'] = pred_boxes
        
            return pred_dict

        annos = []
        for index, box_dict in enumerate(pred_dicts):
            frame_id = batch_dict['frame_id'][index]

            single_pred_dict = generate_single_sample_dict(index, box_dict)
            single_pred_dict['frame_id'] = frame_id
            annos.append(single_pred_dict)

            if output_path is not None:
                cur_det_file = output_path / ('%s.txt' % frame_id)
                with open(cur_det_file, 'w') as f:
                    bbox = single_pred_dict['bbox']
                    loc = single_pred_dict['location']
                    dims = single_pred_dict['dimensions']  # lhw -> hwl

                    for idx in range(len(bbox)):
                        print('%s -1 -1 %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f'
                              % (single_pred_dict['name'][idx], single_pred_dict['alpha'][idx],
                                 bbox[idx][0], bbox[idx][1], bbox[idx][2], bbox[idx][3],
                                 dims[idx][1], dims[idx][2], dims[idx][0], loc[idx][0],
                                 loc[idx][1], loc[idx][2], single_pred_dict['rotation_y'][idx],
                                 single_pred_dict['score'][idx]), file=f)

        return annos

    def evaluation(self, det_annos, class_names, **kwargs):
        if 'annos' not in self.coda_infos[0].keys():
            return None, {}

        from ..kitti.kitti_object_eval_python import eval as kitti_eval

        eval_det_annos = copy.deepcopy(det_annos)
        eval_gt_annos = [copy.deepcopy(info['annos']) for info in self.coda_infos]
        ap_result_str, ap_dict = kitti_eval.get_official_eval_result(eval_gt_annos, eval_det_annos, class_names)

        return ap_result_str, ap_dict

    def __len__(self):
        if self._merge_all_iters_to_one_epoch:
            return len(self.coda_infos) * self.total_epochs

        return len(self.coda_infos)

    def __getitem__(self, index):
        # index = 4
        if self._merge_all_iters_to_one_epoch:
            index = index % len(self.coda_infos)
        
        if self.use_sorted_imageset:
            matching_lidar_idx = self.sorted_lidar_idx_map[index]
            info = copy.deepcopy(self.coda_infos[matching_lidar_idx])
        else:
            info = copy.deepcopy(self.coda_infos[index])
        # print("before if name annos ", info['annos']['name'].shape)
        # print("before if bbox annos ", info['annos']['bbox'].shape)
        sample_idx = info['point_cloud']['lidar_idx']
        # print(f'Requested index {index} LiDAR idx {sample_idx}') 
        points = self.get_lidar(sample_idx)
        calib = self.get_calib(sample_idx)

        img_shape = info['image']['image_shape']
        if self.dataset_cfg.FOV_POINTS_ONLY:
            pts_rect = calib.lidar_to_rect(points[:, 0:3])
            fov_flag = self.get_fov_flag(pts_rect, img_shape, calib)
            points = points[fov_flag]

        if self.dataset_cfg.get('SHIFT_COOR', None):
            points[:, 0:3] += np.array(self.dataset_cfg.SHIFT_COOR, dtype=np.float32)

        input_dict = {
            'points': points,
            'frame_id': sample_idx,
            'calib': calib,
            'image_shape': img_shape
        }

        if 'annos' in info:
            annos = info['annos']
            # print("if name annos ", annos['name'].shape)
            # print("if bbox annos ", annos['bbox'].shape)
            annos = common_utils.drop_info_with_name(annos, name='DontCare', gt_filtered=True)
            loc, dims, rots = annos['location'], annos['dimensions'], annos['rotation_y']
            gt_names = annos['name']
            gt_boxes_camera = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1).astype(np.float32)
            gt_boxes_lidar = box_utils.boxes3d_kitti_camera_to_lidar(gt_boxes_camera, calib)

            if self.dataset_cfg.get('SHIFT_COOR', None):
                gt_boxes_lidar[:, 0:3] += self.dataset_cfg.SHIFT_COOR

            input_dict.update({
                'gt_names': gt_names,
                'gt_boxes': gt_boxes_lidar
            })

            if self.dataset_cfg.get('REMOVE_ORIGIN_GTS', None) and self.training:
                input_dict['points'] = box_utils.remove_points_in_boxes3d(input_dict['points'], input_dict['gt_boxes'])
                mask = np.zeros(gt_boxes_lidar.shape[0], dtype=np.bool_)
                input_dict['gt_boxes'] = input_dict['gt_boxes'][mask]
                input_dict['gt_names'] = input_dict['gt_names'][mask]

            if self.dataset_cfg.get('USE_PSEUDO_LABEL', None) and self.training:
                input_dict['gt_boxes'] = None

            # for debug only
            # gt_boxes_mask = np.array([n in self.class_names for n in input_dict['gt_names']], dtype=np.bool_)
            # debug_dict = {'gt_boxes': copy.deepcopy(gt_boxes_lidar[gt_boxes_mask])}

            road_plane = self.get_road_plane(sample_idx)
            if road_plane is not None:
                input_dict['road_plane'] = road_plane

        # load saved pseudo label for unlabel data
        if self.dataset_cfg.get('USE_PSEUDO_LABEL', None) and self.training:
            self.fill_pseudo_labels(input_dict)

        data_dict = self.prepare_data(data_dict=input_dict)

        return data_dict


def create_coda_infos(dataset_cfg, class_names, data_path, save_path, workers=8):
    dataset = CODataset(dataset_cfg=dataset_cfg, class_names=class_names, root_path=data_path, training=False)
    train_split, val_split = 'train', 'val'

    train_filename = save_path / ('coda_infos_%s.pkl' % train_split)
    val_filename = save_path / ('coda_infos_%s.pkl' % val_split)
    trainval_filename = save_path / 'coda_infos_trainval.pkl'
    test_filename = save_path / 'coda_infos_test.pkl'

    print('---------------Start to generate data infos----3-----------')
    
    dataset.set_split(train_split)
    coda_infos_train = dataset.get_infos(num_workers=workers, has_label=True, count_inside_pts=True)
    with open(train_filename, 'wb') as f:
        pickle.dump(coda_infos_train, f)
    print('CODa info train file is saved to %s' % train_filename)

    dataset.set_split(val_split)
    coda_infos_val = dataset.get_infos(num_workers=workers, has_label=True, count_inside_pts=True)
    with open(val_filename, 'wb') as f:
        pickle.dump(coda_infos_val, f)
    print('CODa info val file is saved to %s' % val_filename)

    with open(trainval_filename, 'wb') as f:
        pickle.dump(coda_infos_train + coda_infos_val, f)
    print('CODa info trainval file is saved to %s' % trainval_filename)

    dataset.set_split('test')
    coda_infos_test = dataset.get_infos(num_workers=workers, has_label=True, count_inside_pts=True)
    with open(test_filename, 'wb') as f:
        pickle.dump(coda_infos_test, f)
    print('CODa info test file is saved to %s' % test_filename)

    print('---------------Start create groundtruth database for data augmentation---------------')
    dataset.set_split(train_split)
    dataset.create_groundtruth_database(train_filename, split=train_split)

    print('---------------Data preparation Done---------------')


if __name__ == '__main__':
    import sys
    if sys.argv.__len__() > 1 and sys.argv[1] == 'create_coda_infos':
        import yaml
        from pathlib import Path
        from easydict import EasyDict
        dataset_cfg = EasyDict(yaml.safe_load(open(sys.argv[2])))
        ROOT_DIR = (Path(__file__).resolve().parent / '../../../').resolve()
        create_coda_infos(
            dataset_cfg=dataset_cfg,
            # class_names=['Car', 'Pedestrian', 'Cyclist'],
            # data_path=ROOT_DIR / 'data' / 'coda128_3class_full',
            # save_path=ROOT_DIR / 'data' / 'coda128_3class_full'
            class_names=[
                'Car',
                'Pedestrian',
                'Cyclist',
                'Motorcycle',
                'Scooter',
                'Tree',
                'TrafficSign',
                'Canopy',
                'TrafficLight',
                'BikeRack',
                'Bollard',
                'ConstructionBarrier',
                'ParkingKiosk',
                'Mailbox',
                'FireHydrant',
                'FreestandingPlant',
                'Pole',
                'InformationalSign',
                'Door',
                'Fence',
                'Railing',
                'Cone',
                'Chair',
                'Bench',
                'Table',
                'TrashCan',
                'NewspaperDispenser',
                'RoomLabel',
                'Stanchion',
                'SanitizerDispenser',
                'CondimentDispenser',
                'VendingMachine',
                'EmergencyAidKit',
                'FireExtinguisher',
                'Computer',
                'Television',
                'Other',
                'PickupTruck',  
                'DeliveryTruck', 
                'ServiceVehicle', 
                'UtilityVehicle',
                'FireAlarm',
                'ATM',
                'Cart',
                'Couch',
                'TrafficArm',
                'WallSign',
                'FloorSign',
                'DoorSwitch',
                'EmergencyPhone',
                'Dumpster',
                'VacuumCleaner',
                'Segway',
                'Bus',
                'Skateboard',
                'WaterFountain'
            ],
            data_path=ROOT_DIR / 'data' / 'coda32_allclass_full',
            save_path=ROOT_DIR / 'data' / 'coda32_allclass_full',
        )
"""
Full Class List
[
'Car',
'Pedestrian',
'Cyclist',
'PickupTruck',  
'DeliveryTruck', 
'ServiceVehicle', 
'UtilityVehicle',
'Scooter',
'Motorcycle',
'FireHydrant',
'FireAlarm',
'ParkingKiosk',
'Mailbox',
'NewspaperDispenser',
'SanitizerDispenser',
'CondimentDispenser',
'ATM',
'VendingMachine',
'DoorSwitch',
'EmergencyAidKit',
'Computer',
'Television',
'Dumpster',
'TrashCan',
'VacuumCleaner',
'Cart',
'Chair',
'Couch',
'Bench',
'Table',
'Bollard',
'ConstructionBarrier',
'Fence',
'Railing',
'Cone',
'Stanchion',
'TrafficLight',
'TrafficSign',
'TrafficArm',
'Canopy',
'BikeRack',
'Pole',
'InformationalSign',
'WallSign',
'Door',
'FloorSign',
'RoomLabel',
'FreestandingPlant',
'Tree',
'Other'
]
"""
