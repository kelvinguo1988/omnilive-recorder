"""数据库模型定义"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Room(Base):
    """直播间房间"""
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(500), nullable=False, comment="直播间地址")
    platform = Column(String(50), nullable=False, comment="平台: douyin/bilibili/kuaishou")
    room_id = Column(String(100), nullable=True, comment="房间ID")
    title = Column(String(200), nullable=True, comment="直播标题")
    streamer_name = Column(String(100), nullable=True, comment="主播名称")
    quality = Column(String(50), default="origin", comment="录制画质")
    enabled = Column(Boolean, default=True, comment="是否启用监控")
    is_live = Column(Boolean, default=False, comment="是否正在直播")
    is_recording = Column(Boolean, default=False, comment="是否正在录制")
    last_check_time = Column(DateTime, nullable=True, comment="最后检测时间")
    last_live_time = Column(DateTime, nullable=True, comment="最后直播时间")
    remark = Column(String(200), nullable=True, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment="更新时间")

    recordings = relationship("Recording", back_populates="room", cascade="all, delete-orphan")


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
    started_at = Column(DateTime, default=datetime.utcnow, comment="开始时间")
    ended_at = Column(DateTime, nullable=True, comment="结束时间")
    error_message = Column(Text, nullable=True, comment="错误信息")

    room = relationship("Room", back_populates="recordings")


class SystemLog(Base):
    """系统日志"""
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(20), default="info", comment="日志级别")
    module = Column(String(50), nullable=True, comment="模块")
    message = Column(Text, nullable=False, comment="日志内容")
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")
