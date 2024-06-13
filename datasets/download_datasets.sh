"""
将文件解压到datasets/文件夹
"""

wget -c https://storage.yandexcloud.net/yandex-research/ddpm-segmentation/datasets.tar.gz
tar -xzf datasets.tar.gz -C datasets/
rm datasets.tar.gz
