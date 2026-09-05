"""数据库模型定义"""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Room(Base):
    """主播（统一实体：直播间地址用于直播录制，主页地址用于作品订阅）

    表名保持 rooms 以兼容历史录制记录外键；语义已从"直播间"升级为"主播"：
    - url           直播间地址（空串=未配置，不监控直播）
    - home_url      主页地址（空串=未配置；提供 platform_user_id 即可订阅作品）
    - platform_user_id 平台用户ID(sec_uid/mid/principal)，直播间检测自动回填，
      或由主页地址 URL 解析得到；作品订阅依赖此字段
    """
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(500), nullable=False, default="", comment="直播间地址(空=未配置直播监控)")
    platform = Column(String(50), nullable=False, comment="平台: douyin/bilibili/kuaishou")
    room_id = Column(String(100), nullable=True, comment="直播间房间号/短ID")
    home_url = Column(String(500), nullable=True, comment="主播主页地址(空=未配置作品订阅)")
    platform_user_id = Column(String(150), nullable=True, comment="平台用户ID(sec_uid/mid/principal)")
    works_enabled = Column(Boolean, default=False, comment="是否启用作品订阅")
    backfill_done = Column(Boolean, default=False, comment="历史作品是否已全部回填")
    last_work_check_time = Column(DateTime, nullable=True, comment="最后作品检查时间")
    title = Column(String(200), nullable=True, comment="直播标题")
    streamer_name = Column(String(100), nullable=True, comment="主播名称")
    quality = Column(String(50), default="origin", comment="录制画质")
    enabled = Column(Boolean, default=True, comment="是否启用（直播监控+作品订阅总开关）")
    is_live = Column(Boolean, default=False, comment="是否正在直播")
    is_recording = Column(Boolean, default=False, comment="是否正在录制")
    last_check_time = Column(DateTime, nullable=True, comment="最后直播检测时间")
    last_live_time = Column(DateTime, nullable=True, comment="最后直播时间")
    remark = Column(String(200), nullable=True, comment="备注")
    created_at = Column(DateTime, default=datetime.now(timezone.utc).replace(tzinfo=None), comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now(timezone.utc).replace(tzinfo=None), onupdate=datetime.now(timezone.utc).replace(tzinfo=None), comment="更新时间")

    recordings = relationship("Recording", back_populates="room", cascade="all, delete-orphan")
    works = relationship("Work", back_populates="room", cascade="all, delete-orphan")


class Recording(Base):
    """录制记录"""
    __tablename__ = "recordings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, comment="房间ID")
    file_path = Column(String(500), nullable=True, comment="文件路径")
    file_name = Column(String(200), nullable=True, comment="文件名")
    file_size = Column(Integer, default=0, comment="文件大小(字节)")
    duration = Column(Float, default=0, comment="录制时长(秒)")
    format = Column(String(10), default="ts", comment="文件格式")
    status = Column(String(20), default="pending", comment="状态: recording/completed/failed")
    started_at = Column(DateTime, default=datetime.now(timezone.utc).replace(tzinfo=None), comment="开始时间")
    ended_at = Column(DateTime, nullable=True, comment="结束时间")
    error_message = Column(Text, nullable=True, comment="错误信息")
    part_paths = Column(Text, nullable=True, comment="多段录制part路径列表(JSON)，断流重连时追加")

    room = relationship("Room", back_populates="recordings")


class SystemLog(Base):
    """系统日志"""
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(20), default="info", comment="日志级别")
    module = Column(String(50), nullable=True, comment="模块")
    message = Column(Text, nullable=False, comment="日志内容")
    created_at = Column(DateTime, default=datetime.now(timezone.utc).replace(tzinfo=None), comment="创建时间")


class Work(Base):
    """创作者作品（视频/图集）。creator_id 即所属主播 rooms.id（统一主播模型）。"""
    __tablename__ = "works"
    __table_args__ = (
        UniqueConstraint("creator_id", "platform_work_id", name="uq_work_creator_wid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    creator_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, comment="所属主播(rooms.id)")
    platform_work_id = Column(String(150), nullable=False, comment="平台作品ID(aweme_id/bvid/photo_id)")
    work_type = Column(String(20), default="video", comment="类型: video/images")
    title = Column(String(500), nullable=True, comment="标题/描述")
    publish_time = Column(DateTime, nullable=True, comment="发布时间")
    duration = Column(Float, default=0, comment="时长(秒)，图集为0")
    file_path = Column(String(600), nullable=True, comment="下载文件相对路径")
    file_size = Column(Integer, default=0, comment="文件大小(字节)")
    download_urls = Column(Text, nullable=True, comment="下载地址JSON列表（列表接口已解析的直链）")
    status = Column(String(20), default="pending", comment="状态: pending/downloading/completed/failed")
    error_message = Column(Text, nullable=True, comment="失败原因")
    downloaded_at = Column(DateTime, nullable=True, comment="下载完成时间")
    created_at = Column(DateTime, default=datetime.now(timezone.utc).replace(tzinfo=None), comment="入库时间")

    room = relationship("Room", back_populates="works")
