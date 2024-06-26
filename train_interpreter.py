'''
这个程序的最终目的是通过训练和评估一个像素分类器模型来进行图像语义分割。图像语义分割是一种任务，其中目标是将图像的每个像素分类为特定类别。

**数据准备 (prepare_data)**：
通过指定的特征提取器从原始图像数据集中提取特征。
生成特征张量 (X) 和对应的标签张量 (y)，并处理标签中标注数量较少的异常情况。
**模型训练 (train)**：
使用上一步准备的数据，创建一个特征数据集。
将该数据集加载到一个数据加载器中，并在多个epoch（训练轮次）中迭代，使用交叉熵损失函数和Adam优化器来训练一个像素分类器模型。
动态调整训练过程，保存训练好的模型权重。
**模型评估 (evaluation)**：
通过特征提取器从测试数据集中提取特征。
使用训练好的模型对测试图像进行像素级分类，生成预测标签。
计算和输出整体mIoU（平均交并比），这是衡量语义分割模型表现的一个指标，还计算预测的不确定度。

'''
import torch
import torch.nn as nn
from tqdm import tqdm
import json
import os
import gc

from torch.utils.data import DataLoader

import argparse
from src.utils import setup_seed, multi_acc
from src.pixel_classifier import load_ensemble, compute_iou, predict_labels, save_predictions, pixel_classifier
from src.datasets import ImageLabelDataset, FeatureDataset, make_transform
from src.feature_extractors import create_feature_extractor, collect_features

from guided_diffusion.guided_diffusion.script_util import model_and_diffusion_defaults, add_dict_to_argparser
from guided_diffusion.guided_diffusion.dist_util import dev

# 准备数据集
def prepare_data(args):
    # 创建特征提取器
    feature_extractor = create_feature_extractor(**args)
    
    print(f"准备训练集: {args['category']}...")
    # 创建图像标签数据集
    dataset = ImageLabelDataset(
        data_dir=args['training_path'],
        resolution=args['image_size'],
        num_images=args['training_number'],
        transform=make_transform(args['model_type'], args['image_size'])
    )
    
    # 初始化特征和标签张量
    X = torch.zeros((len(dataset), *args['dim'][::-1]), dtype=torch.float)
    y = torch.zeros((len(dataset), *args['dim'][:-1]), dtype=torch.uint8)

    # 如果指定了共享噪声
    if 'share_noise' in args and args['share_noise']:
        rnd_gen = torch.Generator(device=dev()).manual_seed(args['seed'])
        noise = torch.randn(1, 3, args['image_size'], args['image_size'], 
                            generator=rnd_gen, device=dev())
    else:
        noise = None 

    for row, (img, label) in enumerate(tqdm(dataset)):
        img = img[None].to(dev())
        features = feature_extractor(img, noise=noise)
        X[row] = collect_features(args, features).cpu()
        
        # 删除标注数量少于20的标签
        for target in range(args['number_class']):
            if target == args['ignore_label']: continue
            if 0 < (label == target).sum() < 20:
                print(f'删除小于20的标注: 图片 {dataset.image_paths[row]} 标签 {target}')
                label[label == target] = args['ignore_label']
        y[row] = label
    
    d = X.shape[1]
    print(f'总维度 {d}')
    X = X.permute(1,0,2,3).reshape(d, -1).permute(1, 0)
    y = y.flatten()

    return X[y != args['ignore_label']], y[y != args['ignore_label']]


# 评估模型
def evaluation(args, models):
    feature_extractor = create_feature_extractor(**args)
    dataset = ImageLabelDataset(
        data_dir=args['testing_path'],
        resolution=args['image_size'],
        num_images=args['testing_number'],
        transform=make_transform(args['model_type'], args['image_size'])
    )

    if 'share_noise' in args and args['share_noise']:
        rnd_gen = torch.Generator(device=dev()).manual_seed(args['seed'])
        noise = torch.randn(1, 3, args['image_size'], args['image_size'], 
                            generator=rnd_gen, device=dev())
    else:
        noise = None 

    preds, gts, uncertainty_scores = [], [], []
    for img, label in tqdm(dataset):        
        img = img[None].to(dev())
        features = feature_extractor(img, noise=noise)
        features = collect_features(args, features)

        x = features.view(args['dim'][-1], -1).permute(1, 0)
        pred, uncertainty_score = predict_labels(models, x, size=args['dim'][:-1])
        gts.append(label.numpy())
        preds.append(pred.numpy())
        uncertainty_scores.append(uncertainty_score.item())
    
    save_predictions(args, dataset.image_paths, preds)
    miou = compute_iou(args, preds, gts)
    print(f'总mIoU: ', miou)
    print(f'不确定性均值: {sum(uncertainty_scores) / len(uncertainty_scores)}')


# 训练模型
def train(args):
    features, labels = prepare_data(args)
    train_data = FeatureDataset(features, labels)

    print(f" ********* 最大标签 {args['number_class']} *** 忽略标签 {args['ignore_label']} ***********")
    print(f" *********************** 当前数据数量 {len(features)} ***********************")

    train_loader = DataLoader(dataset=train_data, batch_size=args['batch_size'], shuffle=True, drop_last=True)

    print(" *********************** 当前数据加载器长度 " +  str(len(train_loader)) + " ***********************")
    for MODEL_NUMBER in range(args['start_model_num'], args['model_num'], 1):

        gc.collect()
        classifier = pixel_classifier(numpy_class=(args['number_class']), dim=args['dim'][-1])
        classifier.init_weights()

        classifier = nn.DataParallel(classifier).cuda()
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(classifier.parameters(), lr=0.001)
        classifier.train()

        iteration = 0
        break_count = 0
        best_loss = 10000000
        stop_sign = 0
        for epoch in range(100):
            for X_batch, y_batch in train_loader:
                X_batch, y_batch = X_batch.to(dev()), y_batch.to(dev())
                y_batch = y_batch.type(torch.long)

                optimizer.zero_grad()
                y_pred = classifier(X_batch)
                loss = criterion(y_pred, y_batch)
                acc = multi_acc(y_pred, y_batch)

                loss.backward()
                optimizer.step()

                iteration += 1
                if iteration % 1000 == 0:
                    print('Epoch : ', str(epoch), 'iteration', iteration, 'loss', loss.item(), 'acc', acc)
                
                if epoch > 3:
                    if loss.item() < best_loss:
                        best_loss = loss.item()
                        break_count = 0
                    else:
                        break_count += 1

                    if break_count > 50:
                        stop_sign = 1
                        print("*************** 终止, 总迭代次数,", iteration, ", 在 epoch", str(epoch), "***************")
                        break

            if stop_sign == 1:
                break

        model_path = os.path.join(args['exp_dir'], 
                                  'model_' + str(MODEL_NUMBER) + '.pth')
        MODEL_NUMBER += 1
        print('保存到:',model_path)
        torch.save({'model_state_dict': classifier.state_dict()}, model_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, model_and_diffusion_defaults())

    parser.add_argument('--exp', type=str)
    parser.add_argument('--seed', type=int,  default=0)

    args = parser.parse_args()
    setup_seed(args.seed)

    # 加载实验配置
    opts = json.load(open(args.exp, 'r'))
    opts.update(vars(args))
    opts['image_size'] = opts['dim'][0]

    # 准备实验文件夹 
    if len(opts['steps']) > 0:
        suffix = '_'.join([str(step) for step in opts['steps']])
        suffix += '_' + '_'.join([str(step) for step in opts['blocks']])
        opts['exp_dir'] = os.path.join(opts['exp_dir'], suffix)

    path = opts['exp_dir']
    os.makedirs(path, exist_ok=True)
    print('实验文件夹: %s' % (path))
    os.system('cp %s %s' % (args.exp, opts['exp_dir']))

    # 检查是否所有的模型都已经被训练 
    pretrained = [os.path.exists(os.path.join(opts['exp_dir'], f'model_{i}.pth')) 
                  for i in range(opts['model_num'])]
              
    if not all(pretrained):
        # 训练所有剩余的模型
        opts['start_model_num'] = sum(pretrained)
        train(opts)
    
    print('加载预训练模型...')
    models = load_ensemble(opts, device='cuda')
    evaluation(opts, models)
