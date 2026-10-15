"""源码版本，文件和 API 共享同一来源。"""
from pathlib import Path

VERSION=Path(__file__).parents[1].joinpath('VERSION').read_text(encoding='utf-8').strip().split('=',1)[-1]
