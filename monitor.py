'''
这个模块在直播用的codebase中是可以运行的。但是，还没有对开源版本进行适配。
'''
import asyncio
import json
import os
from config import MONITOR_SERVER_PORT, get_character_data
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn
from fastapi.templating import Jinja2Templates
from utils.frontend_utils import find_models, find_model_config_file
templates = Jinja2Templates(directory="./")

app = FastAPI()

# 挂载静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/streamer")
async def get_stream():
    return FileResponse('templates/streamer.html')

@app.get("/subtitle")
async def get_subtitle():
    return FileResponse('templates/subtitle.html')

@app.get('/api/live2d/emotion_mapping/{model_name}')
async def get_emotion_mapping(model_name: str):
    """获取情绪映射配置"""
    try:
        # 在模型目录中查找.model3.json文件
        model_dir = os.path.join('static', model_name)
        if not os.path.exists(model_dir):
            return JSONResponse(status_code=404, content={"success": False, "error": "模型目录不存在"})
        
        # 查找.model3.json文件
        model_json_path = None
        for file in os.listdir(model_dir):
            if file.endswith('.model3.json'):
                model_json_path = os.path.join(model_dir, file)
                break
        
        if not model_json_path or not os.path.exists(model_json_path):
            return JSONResponse(status_code=404, content={"success": False, "error": "模型配置文件不存在"})
        
        with open(model_json_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)

        # 优先使用 EmotionMapping；若不存在则从 FileReferences 推导
        emotion_mapping = config_data.get('EmotionMapping')
        if not emotion_mapping:
            derived_mapping = {"motions": {}, "expressions": {}}
            file_refs = config_data.get('FileReferences', {}) or {}

            # 从标准 Motions 结构推导
            motions = file_refs.get('Motions', {}) or {}
            for group_name, items in motions.items():
                files = []
                for item in items or []:
                    try:
                        file_path = item.get('File') if isinstance(item, dict) else None
                        if file_path:
                            files.append(file_path.replace('\\', '/'))
                    except Exception:
                        continue
                derived_mapping["motions"][group_name] = files

            # 从标准 Expressions 结构推导（按 Name 的前缀进行分组，如 happy_xxx）
            expressions = file_refs.get('Expressions', []) or []
            for item in expressions:
                if not isinstance(item, dict):
                    continue
                name = item.get('Name') or ''
                file_path = item.get('File') or ''
                if not file_path:
                    continue
                file_path = file_path.replace('\\', '/')
                # 根据第一个下划线拆分分组
                if '_' in name:
                    group = name.split('_', 1)[0]
                else:
                    # 无前缀的归入 neutral 组，避免丢失
                    group = 'neutral'
                derived_mapping["expressions"].setdefault(group, []).append(file_path)

            emotion_mapping = derived_mapping
        
        return {"success": True, "config": emotion_mapping}
    except Exception as e:
        print(f"获取情绪映射配置失败: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/{lanlan_name}", response_class=HTMLResponse)
async def get_index(request: Request, lanlan_name: str):
    # 获取角色配置
    _, _, _, lanlan_basic_config, _, _, _, _, _, _ = get_character_data()
    # 获取live2d字段
    live2d = lanlan_basic_config.get(lanlan_name, {}).get('live2d', 'mao_pro')
    # 查找所有模型
    models = find_models()
    # 根据live2d字段查找对应的model path
    model_path = next((m["path"] for m in models if m["name"] == live2d), find_model_config_file(live2d))
    return templates.TemplateResponse("templates/viewer.html", {
        "request": request,
        "lanlan_name": lanlan_name,
        "model_path": model_path
    })


# 存储所有连接的客户端
connected_clients = set()
subtitle_clients = set()
current_subtitle = ""
should_clear_next = False

def is_japanese(text):
    import re
    # 检测平假名、片假名、汉字
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))

# 简单的日文到中文翻译（这里需要你集成实际的翻译API）
async def translate_japanese_to_chinese(text):
    # 为了演示，这里返回一个占位符
    # 你需要根据实际情况实现翻译功能
    pass

@app.websocket("/subtitle_ws")
async def subtitle_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print(f"字幕客户端已连接: {websocket.client}")

    # 添加到字幕客户端集合
    subtitle_clients.add(websocket)

    try:
        # 发送当前字幕（如果有）
        if current_subtitle:
            await websocket.send_json({
                "type": "subtitle",
                "text": current_subtitle
            })

        # 保持连接
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        print(f"字幕客户端已断开: {websocket.client}")
    finally:
        subtitle_clients.discard(websocket)


# 广播字幕到所有字幕客户端
async def broadcast_subtitle():
    global current_subtitle, should_clear_next
    if should_clear_next:
        await clear_subtitle()
        should_clear_next = False
        # 给一个短暂的延迟让清空动画完成
        await asyncio.sleep(0.3)

    clients = subtitle_clients.copy()
    for client in clients:
        try:
            await client.send_json({
                "type": "subtitle",
                "text": current_subtitle
            })
        except Exception as e:
            print(f"字幕广播错误: {e}")
            subtitle_clients.discard(client)


# 清空字幕
async def clear_subtitle():
    global current_subtitle
    current_subtitle = ""

    clients = subtitle_clients.copy()
    for client in clients:
        try:
            await client.send_json({
                "type": "clear"
            })
        except Exception as e:
            print(f"清空字幕错误: {e}")
            subtitle_clients.discard(client)

# 主服务器连接端点
@app.websocket("/sync/{lanlan_name}")
async def sync_endpoint(websocket: WebSocket, lanlan_name:str):
    await websocket.accept()
    print(f"✅ [SYNC] 主服务器已连接: {websocket.client}")

    try:
        while True:
            try:
                global current_subtitle
                data = await asyncio.wait_for(websocket.receive_text(), timeout=25)

                # 广播到所有连接的客户端
                data = json.loads(data)
                msg_type = data.get("type", "unknown")


                if msg_type == "gemini_response":
                    # 发送到字幕显示
                    subtitle_text = data.get("text", "")
                    current_subtitle += subtitle_text
                    if subtitle_text:
                        await broadcast_subtitle()

                elif msg_type == "turn end":
                    # 处理回合结束
                    if current_subtitle:
                        # 检查是否为日文，如果是则翻译
                        if is_japanese(current_subtitle):
                            translated_text = await translate_japanese_to_chinese(current_subtitle)
                            current_subtitle = translated_text
                            clients = subtitle_clients.copy()
                            for client in clients:
                                try:
                                    await client.send_json({
                                        "type": "subtitle",
                                        "text": translated_text
                                    })
                                except Exception as e:
                                    print(f"翻译字幕广播错误: {e}")
                                    subtitle_clients.discard(client)

                    # 清空字幕区域，准备下一条
                    global should_clear_next
                    should_clear_next = True

                if msg_type != "heartbeat":
                    await broadcast_message(data)
            except asyncio.exceptions.TimeoutError:
                pass
    except WebSocketDisconnect:
        print(f"❌ [SYNC] 主服务器已断开: {websocket.client}")
    except Exception as e:
        print(f"❌ [SYNC] 同步端点错误: {e}")
        import traceback
        traceback.print_exc()


# 二进制数据同步端点
@app.websocket("/sync_binary/{lanlan_name}")
async def sync_binary_endpoint(websocket: WebSocket, lanlan_name:str):
    await websocket.accept()
    print(f"✅ [BINARY] 主服务器二进制连接已建立: {websocket.client}")

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_bytes(), timeout=25)
                if len(data)>4:
                    await broadcast_binary(data)
            except asyncio.exceptions.TimeoutError:
                pass
    except WebSocketDisconnect:
        print(f"❌ [BINARY] 主服务器二进制连接已断开: {websocket.client}")
    except Exception as e:
        print(f"❌ [BINARY] 二进制同步端点错误: {e}")
        import traceback
        traceback.print_exc()


# 客户端连接端点
@app.websocket("/ws/{lanlan_name}")
async def websocket_endpoint(websocket: WebSocket, lanlan_name:str):
    await websocket.accept()
    print(f"✅ [CLIENT] 查看客户端已连接: {websocket.client}, 当前总数: {len(connected_clients) + 1}")

    # 添加到连接集合
    connected_clients.add(websocket)

    try:
        # 保持连接直到客户端断开
        while True:
            # 接收任何类型的消息（文本或二进制），主要用于保持连接
            try:
                await websocket.receive_text()
            except:
                # 如果收到的是二进制数据，receive_text() 会失败，尝试 receive_bytes()
                try:
                    await websocket.receive_bytes()
                except:
                    # 如果两者都失败，等待一下再继续
                    await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        print(f"❌ [CLIENT] 查看客户端已断开: {websocket.client}")
    except Exception as e:
        print(f"❌ [CLIENT] 客户端连接异常: {e}")
    finally:
        # 安全地移除客户端（即使已经被移除也不会报错）
        connected_clients.discard(websocket)
        print(f"🗑️ [CLIENT] 已移除客户端，当前剩余: {len(connected_clients)}")


# 广播消息到所有客户端
async def broadcast_message(message):
    clients = connected_clients.copy()
    success_count = 0
    fail_count = 0
    disconnected_clients = []
    
    for client in clients:
        try:
            await client.send_json(message)
            success_count += 1
        except Exception as e:
            print(f"❌ [BROADCAST] 广播错误到 {client.client}: {e}")
            fail_count += 1
            disconnected_clients.append(client)
    
    # 移除所有断开的客户端
    for client in disconnected_clients:
        connected_clients.discard(client)
        print(f"🗑️ [BROADCAST] 移除断开的客户端: {client.client}")
    
    if success_count > 0:
        print(f"✅ [BROADCAST] 成功广播到 {success_count} 个客户端" + (f", 失败并移除 {fail_count} 个" if fail_count > 0 else ""))


# 广播二进制数据到所有客户端
async def broadcast_binary(data):
    clients = connected_clients.copy()
    success_count = 0
    fail_count = 0
    disconnected_clients = []
    
    for client in clients:
        try:
            await client.send_bytes(data)
            success_count += 1
        except Exception as e:
            print(f"❌ [BINARY BROADCAST] 二进制广播错误到 {client.client}: {e}")
            fail_count += 1
            disconnected_clients.append(client)
    
    # 移除所有断开的客户端
    for client in disconnected_clients:
        connected_clients.discard(client)
        print(f"🗑️ [BINARY BROADCAST] 移除断开的客户端: {client.client}")
    
    if success_count > 0:
        print(f"✅ [BINARY BROADCAST] 成功广播音频到 {success_count} 个客户端" + (f", 失败并移除 {fail_count} 个" if fail_count > 0 else ""))


# 定期清理断开的连接
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_disconnected_clients())


async def cleanup_disconnected_clients():
    while True:
        try:
            # 检查并移除已断开的客户端
            for client in list(connected_clients):
                try:
                    await client.send_json({"type": "heartbeat"})
                except Exception as e:
                    print("广播错误:", e)
                    connected_clients.remove(client)
            await asyncio.sleep(60)  # 每分钟检查一次
        except Exception as e:
            print(f"清理客户端错误: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    uvicorn.run("monitor:app", host="0.0.0.0", port=MONITOR_SERVER_PORT, reload=True)
