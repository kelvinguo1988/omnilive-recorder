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


# 记录当前使用的配置文件路径，供 save_config 写回
CONFIG_PATH: str = os.environ.get("CONFIG_PATH", "/app/config/config.ini")


def load_config(config_path: str = None) -> AppConfig:
    """从INI文件加载配置"""
    global CONFIG_PATH
    config = AppConfig()

    if config_path is None:
        config_path = CONFIG_PATH

    if os.path.exists(config_path):
        parser = configparser.ConfigParser()
        parser.read(config_path, encoding="utf-8")

        # DEFAULT 是 configparser 的保留节，parser["DEFAULT"] 始终可用（无需 has_section 判断）
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

    # 记录实际使用的配置文件路径，供运行时写回
    CONFIG_PATH = config_path

    return config


def save_config(config: AppConfig = None, config_path: str = None) -> bool:
    """将配置写回 INI 文件（持久化，重启后依然生效）

    只持久化可通过配置文件管理的字段；数据库URL等运行时派生项不写入。
    """
    if config is None:
        config = settings
    if config_path is None:
        config_path = CONFIG_PATH

    parser = configparser.ConfigParser()
    if os.path.exists(config_path):
        parser.read(config_path, encoding="utf-8")
    # DEFAULT 是 configparser 的保留节，已内置，不能 add_section，直接写入即可
    section = parser["DEFAULT"]

    def b(v: bool) -> str:
        return "true" if v else "false"

    section["record_format"] = str(config.record_format)
    section["video_quality"] = str(config.video_quality)
    section["segment_time"] = str(config.segment_time)
    section["max_retries"] = str(config.max_retries)
    section["retry_delay"] = str(config.retry_delay)
    section["monitor_interval"] = str(config.monitor_interval)
    section["check_timeout"] = str(config.check_timeout)
    section["output_dir"] = str(config.output_dir)
    section["max_disk_usage"] = str(config.max_disk_usage)
    section["host"] = str(config.host)
    section["port"] = str(config.port)
    section["enable_notification"] = b(config.enable_notification)
    section["webhook_url"] = str(config.webhook_url)
    section["enable_proxy"] = b(config.enable_proxy)
    section["proxy_addr"] = str(config.proxy_addr)
    section["douyin_cookie"] = str(config.douyin_cookie)

    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        parser.write(f)

    # 确保输出目录存在
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    return True


# 全局配置实例
settings = load_config()
