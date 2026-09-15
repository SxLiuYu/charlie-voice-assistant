"""摄像头路由: 源列表 / 按需取帧 / MJPEG 实时流 / 抓帧+视觉分析"""
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger("magic")

router = APIRouter(prefix="/api/camera", tags=["camera"])


@router.get("")
async def list_cameras():
    """列出已配置的摄像头源及状态。"""
    from app import camera
    return {"sources": camera.list_sources()}


@router.get("/frame/{name}")
async def camera_frame(name: str):
    """按需抓一帧 JPEG。name 不存在时 404，抓取失败 503。"""
    from app import camera
    srcs = camera._get_sources()
    if srcs and name not in srcs:
        raise HTTPException(404, f"摄像头源不存在: {name}，可用: {list(srcs)}")
    jpeg = camera.grab_frame(name)
    if not jpeg:
        raise HTTPException(503, f"抓帧失败（源离线或未配置）: {name}")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/stream/{name}")
async def camera_stream(name: str):
    """MJPEG 实时流（浏览器直接 <img src> 可看）。"""
    from app import camera
    srcs = camera._get_sources()
    if not srcs:
        raise HTTPException(404, "未配置摄像头源(CAMERA_SOURCES)")
    if name not in srcs:
        raise HTTPException(404, f"摄像头源不存在: {name}，可用: {list(srcs)}")

    def _gen():
        # 客户端断开时 Starlette 对同步生成器抛 GeneratorExit 自然终止
        for jpg in camera.mjpeg_stream(name):
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + jpg + b"\r\n")

    return StreamingResponse(_gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@router.post("/analyze/{name}")
async def camera_analyze(name: str, body: dict | None = None):
    """抓帧 + 视觉模型分析。body: {"question": "画面里有什么"}"""
    from app import camera
    srcs = camera._get_sources()
    if not srcs:
        raise HTTPException(404, "未配置摄像头源(CAMERA_SOURCES)")
    if name not in srcs:
        raise HTTPException(404, f"摄像头源不存在: {name}，可用: {list(srcs)}")
    question = (body or {}).get("question", "描述画面内容")
    result = camera.analyze_camera(name, question)
    if not result:
        raise HTTPException(503, f"分析失败（源离线或视觉服务不可用）: {name}")
    return {"source": name, "question": question, "answer": result}
