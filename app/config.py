"""应用配置管理"""
import configparser
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class AppConfig(BaseSettings):
    """应用配置"""
    # 录制设置
    record_format: str = "ts"
    video_quality: str = "origin"
    segment_time: int = 1800
    max_retries: int = 3
    retry_delay: int = 10

    # 监控设置
    monitor_interval: int = 120
    check_timeout: int = 15

    # 存储设置
    output_dir: str = "/app/recordings"
    max_disk_usage: int = 90

    # 服务器设置
    host: str = "0.0.0.0"
    port: int = 8000

    # 通知设置
    enable_notification: bool = False
    webhook_url: str = ""

    # 代理设置
    enable_proxy: bool = False
    proxy_addr: str = ""

    # 抖音Cookie
    douyin_cookie: str = ""

    # 数据库
    database_url: str = "sqlite+aiosqlite:////app/data/live_recorder.db"

    class Config:
        env_prefix = "LIVE_RECORDER_"


def get_data_dir(database_url: str = None) -> str:
    """根据数据库URL推导数据目录（容器内外通用）"""
    url = database_url or "sqlite+aiosqlite:////app/data/live_recorder.db"
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if url.startswith("//"):
        url = url[1:]
    return os.path.dirname(url) or "/app/data"


def load_config(config_path: str = None) -> AppConfig:
    """从INI文件加载配置"""
    config = AppConfig()

    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "/app/config/config.ini")

    if os.path.exists(config_path):
        parser = configparser.ConfigParser()
        parser.read(config_path, encoding="utf-8")

        if parser.has_section("DEFAULT"):
            section = parser["DEFAULT"]

            mapping = {
                "record_format": str,
                "video_quality": str,
                "segment_time": int,
                "max_retries": int,
                "retry_delay": int,
                "monitor_interval": int,
                "check_timeout": int,
                "output_dir": str,
                "max_disk_usage": int,
                "host": str,
                "port": int,
                "enable_notification": lambda x: x.lower() == "true",
                "webhook_url": str,
                "enable_proxy": lambda x: x.lower() == "true",
                "proxy_addr": str,
                "douyin_cookie": str,
            }

            for key, converter in mapping.items():
                if key in section:
                    try:
                        value = converter(section[key])
                        setattr(config, key, value)
                    except (ValueError, TypeError):
                        pass

    # 确保输出目录存在
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    Path(get_data_dir(config.database_url)).mkdir(parents=True, exist_ok=True)

    return config


# 全局配置实例
settings = load_config()
