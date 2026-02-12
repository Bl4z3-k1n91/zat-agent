import uvicorn
import time
import os
import json
import socket
import struct
import threading
import uuid
import asyncio
import argparse
import requests
import grpc
from collections import deque
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, List
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# Try importing generated gRPC modules
try:
    import zat_pb2
    import zat_pb2_grpc
except ImportError:
    zat_pb2 = None
    zat_pb2_grpc = None

# --- CONFIGURATION ---
WEB_PORT = 5000
WEB_HOST = "0.0.0.0"
NETFLOW_PORT = 2055
NETFLOW_IP = "0.0.0.0"
GRPC_AGENT_PORT = 50051
AGENT_HTTP_PORT = 8000

parser = argparse.ArgumentParser()
parser.add_argument("--no-flows", action="store_true")
args, _ = parser.parse_known_args()
ENABLE_FLOWS = not args.no_flows

# --- STATE ---
inventory = {}
recent_flows = deque(maxlen=100)
exporters = {} 
sensor_streams = {} 
flow_stats = {"packets": 0, "records": 0, "global_active": ENABLE_FLOWS}

# --- ACCESS CONTROL STATE ---
access_requests = {}
active_websockets: Dict[str, WebSocket] = {} 
data_lock = threading.Lock()

if not os.path.exists("evidence"): os.makedirs("evidence")

# ==========================================
#   gRPC UTILS & CLIENT
# ==========================================
def probe_device_capabilities(target_ip: str) -> List[str]:
    """JIT Discovery: Wakes up agent, connects via gRPC, asks for capabilities."""
    default = ["/hardware/cpu/temp", "/hardware/fan/speed", "/hardware/psu/voltage", "/system/cpu/utilization", "/system/memory/percent"]
    if not zat_pb2: return default
    
    # 1. Wake Up Agent
    try: requests.post(f"http://{target_ip}:{AGENT_HTTP_PORT}/configure", json={"grpc": {"port": GRPC_AGENT_PORT}}, timeout=2)
    except: pass 

    # 2. Dial gRPC
    try:
        with grpc.insecure_channel(f'{target_ip}:{GRPC_AGENT_PORT}') as channel:
            stub = zat_pb2_grpc.TelemetryServiceStub(channel)
            res = stub.GetCapabilities(zat_pb2.Empty(), timeout=3)
            return list(res.supported_sensors)
    except Exception: return default

def grpc_consumer(ip, xpath, period, duration, sub_id):
    """Internal Dashboard Consumer"""
    if not zat_pb2: return
    try:
        channel = grpc.insecure_channel(f'{ip}:{GRPC_AGENT_PORT}')
        stub = zat_pb2_grpc.TelemetryServiceStub(channel)
        req = zat_pb2.SubscriptionRequest(xpath=xpath, period=period, duration=duration)
        try:
            stream = stub.Subscribe(req)
            for data in stream:
                with data_lock:
                    if sub_id not in sensor_streams: 
                        stream.cancel()
                        break
                    sensor_streams[sub_id]["data"].append({"t": data.timestamp, "y": data.value})
                    sensor_streams[sub_id]["status"] = "Streaming"
            with data_lock:
                if sub_id in sensor_streams: sensor_streams[sub_id]["status"] = "Finished"
        except grpc.RpcError as e:
            with data_lock:
                if sub_id in sensor_streams:
                    sensor_streams[sub_id]["status"] = "Agent Unreachable" if e.code() == grpc.StatusCode.UNAVAILABLE else "Error"
    except Exception:
        with data_lock:
            if sub_id in sensor_streams: sensor_streams[sub_id]["status"] = "Error"

async def stream_relay_to_client(websocket: WebSocket, target_ip: str, xpath: str, duration: int):
    """External Client Relay"""
    if not zat_pb2: 
        await websocket.send_json({"status": "STREAM_ERROR", "details": "Server missing Protobufs"})
        return
    try:
        async with grpc.aio.insecure_channel(f'{target_ip}:{GRPC_AGENT_PORT}') as channel:
            stub = zat_pb2_grpc.TelemetryServiceStub(channel)
            req = zat_pb2.SubscriptionRequest(xpath=xpath, period=1, duration=duration)
            
            await websocket.send_json({"status": "STREAM_STARTED", "target": target_ip, "sensor": xpath, "duration": duration})
            
            async for data in stub.Subscribe(req):
                payload = {"timestamp": data.timestamp, "host": data.host, "sensor": data.xpath, "value": data.value}
                try: await websocket.send_json({"type": "data", "payload": payload})
                except: break
            
            try: await websocket.send_json({"status": "STREAM_ENDED", "reason": "Duration Reached"})
            except: pass

    except grpc.aio.AioRpcError as e:
        try: await websocket.send_json({"status": "STREAM_ERROR", "details": f"{e.code()}: {e.details()}"})
        except: pass
    except Exception as e:
        try: await websocket.send_json({"status": "STREAM_ERROR", "details": str(e)})
        except: pass

# ==========================================
#   NETFLOW ENGINE
# ==========================================
class NetFlowListener(threading.Thread):
    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((NETFLOW_IP, NETFLOW_PORT))
        self.daemon = True
    def run(self):
        print(f"🌊 IPFIX Collector listening on UDP {NETFLOW_PORT}...")
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                if not flow_stats["global_active"]: continue
                flow_stats["packets"] += 1
                self.process_packet(data, addr)
            except Exception: pass
    def process_packet(self, data, addr):
        if len(data) < 16: return
        try: ver, length, export_time, seq, domain_id = struct.unpack('!HHIII', data[:16])
        except: return
        exporter_ip = addr[0]
        exporter_key = f"{exporter_ip}:{domain_id}"
        if exporter_key not in exporters:
            name = "Unknown"
            for h, info in inventory.items():
                if info.get("meta", {}).get("source_ip") == exporter_ip: name = h; break
            exporters[exporter_key] = {"active": True, "packets": 0, "last_seen": 0, "hostname": name if name != "Unknown" else exporter_ip, "domain_id": domain_id, "ip": exporter_ip}
        exporters[exporter_key]["packets"] += 1
        exporters[exporter_key]["last_seen"] = time.time()
        if not exporters[exporter_key]["active"]: return
        if len(recent_flows) == 0 or (time.time() - float(recent_flows[0].get('_ts', 0)) > 2):
            recent_flows.appendleft({"_ts": time.time(), "ts": datetime.now().strftime("%H:%M:%S"), "exporter": exporter_ip, "src": "10.0.0.5", "dst": "8.8.8.8", "app": "DNS", "bytes": 128})

# ==========================================
#   API SETUP
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    if ENABLE_FLOWS: NetFlowListener().start()
    yield

app = FastAPI(title="Zat Command Center", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class SubRequest(BaseModel):
    hostname: str
    xpath: str
    period: int = 1
    duration: int = 60 

class ConfigRequest(BaseModel):
    hostname: str = None
    target_ip: str = None
    config_type: str
    payload: dict

class AccessRequest(BaseModel):
    client_id: str
    target_router: str
    sensor_path: str
    duration: int = 30

class ProbeRequest(BaseModel):
    target: str

@app.post("/api/probe")
def api_probe(req: ProbeRequest):
    target_ip = inventory[req.target]["meta"]["source_ip"] if req.target in inventory else req.target
    return {"status": "success", "sensors": probe_device_capabilities(target_ip)}

@app.post("/api/subscribe")
def subscribe_sensor(req: SubRequest):
    hostname = req.hostname
    target_ip = None
    if hostname in inventory: target_ip = inventory[hostname]["meta"]["source_ip"]
    if not target_ip: return {"status": "error", "message": "Agent unreachable"}
    sub_id = f"{req.hostname}-{req.xpath.split('/')[-1]}"
    
    with data_lock:
        if sub_id in sensor_streams and sensor_streams[sub_id].get("status") == "Streaming":
             return {"status": "error", "message": "Stream Active"}
        sensor_streams[sub_id] = {"id": sub_id, "path": req.xpath, "data": deque(maxlen=30), "status": "Provisioning..."}

    try:
        requests.post(f"http://{target_ip}:{AGENT_HTTP_PORT}/configure", json={"grpc": {"port": GRPC_AGENT_PORT}}, timeout=2)
    except: pass

    with data_lock: sensor_streams[sub_id]["status"] = "Connecting..."
    threading.Thread(target=grpc_consumer, args=(target_ip, req.xpath, req.period, req.duration, sub_id), daemon=True).start()
    return {"status": "success", "subscription_id": sub_id}

@app.post("/api/flows/toggle_global")
def toggle_global():
    flow_stats["global_active"] = not flow_stats["global_active"]
    return {"status": "ON" if flow_stats["global_active"] else "OFF"}

@app.post("/api/flows/toggle_exporter")
def toggle_exporter(req: dict):
    key = req.get("key")
    if key in exporters:
        exporters[key]["active"] = not exporters[key]["active"]
        return {"status": "ok"}
    return {"status": "not_found"}

@app.get("/api/data")
def get_dashboard_data():
    exp_list = [{"key": k, **v} for k, v in exporters.items()]
    exp_list.sort(key=lambda x: x['last_seen'], reverse=True)
    sensors_out = {}
    with data_lock:
        for sid, stream in sensor_streams.items():
            sensors_out[sid] = {
                "id": sid, "path": stream["path"], 
                "values": list(stream["data"]), 
                "status": stream.get("status", "Unknown")
            }
    return {"inventory": inventory, "flows": list(recent_flows), "flow_stats": flow_stats, "exporters": exp_list, "sensors": sensors_out}

@app.post("/telemetry")
async def collect_telemetry(request: Request):
    try:
        data = await request.json()
        if "meta" in data:
            data["meta"]["last_seen_ts"] = time.time()
            data["meta"]["source_ip"] = request.client.host
            inventory[data["meta"]["hostname"]] = data
            return {"status": "ack"}
    except: pass
    return {"status": "error"}

@app.post("/api/push_config")
def push_config(req: ConfigRequest):
    agent_ip = req.target_ip or (inventory[req.hostname]["meta"]["source_ip"] if req.hostname in inventory else None)
    if not agent_ip: raise HTTPException(404, "Unknown Agent")
    try:
        res = requests.post(f"http://{agent_ip}:8000/configure", json={req.config_type: req.payload}, timeout=5)
        return {"status": "success", "agent_response": res.json()} if res.ok else {"error": res.text}
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/api/pull_config")
def pull_config(req: ConfigRequest):
    agent_ip = req.target_ip or (inventory[req.hostname]["meta"]["source_ip"] if req.hostname in inventory else None)
    if not agent_ip: raise HTTPException(404, "Agent not found")
    try:
        res = requests.get(f"http://{agent_ip}:8000/config", timeout=5)
        return res.json() if res.ok else {"error": res.text}
    except Exception as e: return {"error": str(e)}

@app.post("/api/access/request")
def request_access(req: AccessRequest):
    req_id = str(uuid.uuid4())[:8]
    target_ip = inventory[req.target_router]["meta"]["source_ip"] if req.target_router in inventory else req.target_router 
    with data_lock:
        access_requests[req_id] = {
            "id": req_id, "client": req.client_id, "target_host": req.target_router, "target_ip": target_ip,
            "xpath": req.sensor_path, "duration": req.duration, "status": "PENDING", "timestamp": datetime.now().strftime("%H:%M:%S")
        }
    return {"status": "submitted", "request_id": req_id}

@app.post("/api/access/approve")
def approve_request(payload: dict):
    req_id = payload.get("request_id")
    action = payload.get("action")
    target_ip = None
    with data_lock:
        if req_id in access_requests:
            access_requests[req_id]["status"] = "APPROVED" if action == "APPROVE" else "DENIED"
            if action == "APPROVE": target_ip = access_requests[req_id]['target_ip']
        else: return {"status": "error"}
    
    if target_ip:
        try: requests.post(f"http://{target_ip}:{AGENT_HTTP_PORT}/configure", json={"grpc": {"port": GRPC_AGENT_PORT}}, timeout=1)
        except: pass
    return {"status": "ok"}

@app.post("/api/access/revoke")
async def revoke_access(payload: dict):
    req_id = payload.get("request_id")
    with data_lock:
        if req_id in access_requests: access_requests[req_id]["status"] = "REVOKED"
    if req_id in active_websockets:
        try:
            await active_websockets[req_id].send_json({"status": "ACCESS_DENIED"})
            await active_websockets[req_id].close()
            del active_websockets[req_id]
        except: pass
    return {"status": "ok"}

@app.get("/api/access/list")
def list_requests(): return access_requests

@app.websocket("/ws/stream/{request_id}")
async def websocket_endpoint(websocket: WebSocket, request_id: str):
    await websocket.accept()
    active_websockets[request_id] = websocket
    req_data = access_requests.get(request_id)
    if not req_data: await websocket.close(); return

    try:
        if req_data["status"] == "PENDING":
            await websocket.send_json({"status": "WAITING_FOR_APPROVAL"})
            for _ in range(60):
                curr = access_requests.get(request_id, {}).get("status")
                if curr == "APPROVED": break
                if curr in ["DENIED", "REVOKED"]: await websocket.send_json({"status": "ACCESS_DENIED"}); await websocket.close(); return
                await asyncio.sleep(1)
            else: await websocket.send_json({"status": "APPROVAL_TIMEOUT"}); await websocket.close(); return

        req_data = access_requests.get(request_id)
        if req_data["status"] == "APPROVED":
            await stream_relay_to_client(websocket, req_data["target_ip"], req_data["xpath"], req_data["duration"])
            await websocket.close()
    finally:
        if request_id in active_websockets: del active_websockets[request_id]

# ==========================================
#   UI & CLIENT SERVING
# ==========================================

# Internal Dashboard (Admin)
HTML_UI = r"""
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>ZAT // COMMAND</title><script src="https://cdn.tailwindcss.com"></script><script src="https://unpkg.com/lucide@latest"></script><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script><style>@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');body{background-color:#050505;color:#e4e4e7;font-family:'JetBrains Mono',monospace;background-image:radial-gradient(rgba(255,255,255,0.03) 1px,transparent 1px);background-size:24px 24px}.glass{background:rgba(15,15,20,0.7);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.05)}.tab-content{display:none;height:100%}#view-dashboard.active,#view-sensors.active,#view-access.active{display:flex}.tab-content.active{display:block}.tab-btn.active{color:#38bdf8;border-bottom:2px solid #38bdf8}.tab-btn-access.active{color:#facc15;border-bottom:2px solid #facc15}::-webkit-scrollbar{width:6px}::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:3px}.overlay-panel{position:absolute;background:rgba(10,10,12,0.85);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;z-index:10}</style></head><body class="h-screen flex flex-col overflow-hidden text-sm"><header class="h-14 border-b border-zinc-800/50 flex items-center justify-between px-6 bg-zinc-950/80 z-50 flex-shrink-0"><div class="flex items-center gap-3"><i data-lucide="zap" class="text-cyan-400 w-5 h-5"></i><h1 class="text-xl font-bold tracking-tighter text-white">ZAT<span class="text-cyan-400 font-normal">AGENT</span></h1></div><div class="flex gap-4"><button onclick="switchTab('dashboard')" id="tab-dashboard" class="tab-btn active px-4 py-4">DASHBOARD</button><button onclick="switchTab('topology')" id="tab-topology" class="tab-btn px-4 py-4">TOPOLOGY</button><button onclick="switchTab('config')" id="tab-config" class="tab-btn px-4 py-4">CONFIG</button><button onclick="switchTab('sensors')" id="tab-sensors" class="tab-btn px-4 py-4">SENSORS</button><button onclick="switchTab('access')" id="tab-access" class="tab-btn tab-btn-access px-4 py-4 text-yellow-500">ACCESS</button></div><div class="flex items-center gap-4"><div onclick="toggleGlobal()" class="cursor-pointer px-3 py-1 rounded bg-zinc-900 border border-zinc-800 hover:border-zinc-700"><span id="flow-status" class="text-[10px] font-bold text-zinc-500">INIT...</span></div><div id="clock" class="text-zinc-500">00:00</div></div></header><div class="flex-1 relative overflow-hidden"><div id="view-dashboard" class="tab-content active w-full h-full"><div class="w-80 flex-shrink-0 border-r border-zinc-800 bg-zinc-950/50 flex flex-col"><div class="p-6 border-b border-zinc-800"><h2 class="text-lg font-bold text-cyan-400 mb-1">Flow Exporters</h2><div class="text-[10px] text-zinc-500">Manage IPFIX sources</div></div><div class="p-4 border-b border-zinc-800 flex justify-between items-center"><span class="text-xs font-bold text-zinc-500">SOURCES</span><span class="text-[9px] bg-zinc-800 text-zinc-400 px-1.5 rounded" id="exporter-count">0</span></div><div id="exporter-list" class="flex-1 overflow-y-auto p-2 space-y-2"></div></div><div class="flex-1 p-6 relative bg-zinc-900/20 flex flex-col overflow-hidden"><div class="glass w-full h-full rounded-xl flex flex-col overflow-hidden border border-zinc-800/50"><div class="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50"><div class="flex items-center gap-3"><div class="p-2 bg-purple-500/10 rounded-lg border border-purple-500/20"><i data-lucide="activity" class="w-5 h-5 text-purple-400"></i></div><div><h2 class="text-sm font-bold text-white">Live Traffic Feed</h2><div class="text-[10px] text-zinc-500 font-mono">Global Stream</div></div></div><div class="text-right"><div class="text-[10px] text-zinc-500 uppercase font-bold">Packets</div><div class="text-sm font-mono text-white leading-none" id="stat-pkts">0</div></div></div><div class="flex-1 relative overflow-hidden bg-black/20"><div class="absolute inset-0 overflow-y-auto"><table class="w-full text-left border-collapse"><thead class="bg-zinc-900/80 sticky top-0 backdrop-blur-sm z-10 text-[10px] text-zinc-500 uppercase font-bold tracking-wider"><tr><th class="px-6 py-3 border-b border-zinc-800">Time</th><th class="px-6 py-3 border-b border-zinc-800">Exporter</th><th class="px-6 py-3 border-b border-zinc-800">Source</th><th class="px-6 py-3 border-b border-zinc-800">Dest</th><th class="px-6 py-3 border-b border-zinc-800">App</th><th class="px-6 py-3 border-b border-zinc-800 text-right">Bytes</th></tr></thead><tbody id="flow-table" class="font-mono text-xs divide-y divide-zinc-800/50 text-zinc-400"></tbody></table></div></div></div></div></div><div id="view-topology" class="tab-content w-full h-full relative bg-[#050505]"><div class="overlay-panel top-4 left-4 p-4 w-64 shadow-2xl"><div class="flex justify-between items-center mb-3 border-b border-zinc-700 pb-2"><h3 class="text-xs font-bold text-cyan-400 flex items-center gap-2"><i data-lucide="server" class="w-3 h-3"></i> DEVICES</h3><span id="device-count" class="text-[9px] bg-zinc-800 px-1.5 rounded text-zinc-400 font-mono">0</span></div><div id="topology-device-list" class="space-y-1 max-h-60 overflow-y-auto pr-1"></div></div><div id="network-graph" style="width:100%;height:100%"></div></div><div id="view-sensors" class="tab-content w-full h-full bg-[#0c0c0e]"><div class="w-80 flex-shrink-0 border-r border-zinc-800 bg-zinc-950/50 flex flex-col"><div class="p-6 border-b border-zinc-800"><h2 class="text-lg font-bold mb-4 text-cyan-400">New Stream</h2><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Target Router</label><select id="sub-target" onchange="fetchCapabilities(this.value)" class="w-full bg-black border border-zinc-700 p-2 rounded text-white mb-4 text-xs"></select><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Sensor Path</label><select id="sub-path" class="w-full bg-black border border-zinc-700 p-2 rounded text-white mb-4 text-xs"><option value="/hardware/cpu/temp">CPU Temperature</option><option value="/hardware/fan/speed">Fan Speed</option><option value="/hardware/psu/voltage">Voltage</option></select><div class="grid grid-cols-2 gap-2 mb-4"><div><label class="block text-[10px] text-zinc-500 mb-1">Interval (s)</label><input type="number" id="sub-period" value="1" class="w-full bg-black border border-zinc-700 p-2 rounded text-white text-xs"></div><div><label class="block text-[10px] text-zinc-500 mb-1">Duration (s)</label><input type="number" id="sub-dur" value="60" class="w-full bg-black border border-zinc-700 p-2 rounded text-white text-xs"></div></div><button onclick="triggerSubscription()" class="w-full bg-cyan-600 hover:bg-cyan-500 py-2 rounded font-bold text-xs">START STREAM</button></div><div class="p-4 border-b border-zinc-800 text-xs font-bold text-zinc-500">ACTIVE STREAMS</div><div id="stream-list" class="flex-1 overflow-y-auto p-2 space-y-2"></div></div><div class="flex-1 p-6 relative bg-zinc-900/20 flex flex-col overflow-hidden"><div id="chart-wrapper" class="w-full h-full glass rounded-xl p-4 hidden flex-col relative"><div class="flex justify-between items-center mb-4 border-b border-zinc-800 pb-2"><div><h3 id="main-chart-title" class="text-lg font-bold text-white"></h3><div id="main-chart-subtitle" class="text-xs text-zinc-500 font-mono"></div></div><div id="main-chart-status-badge" class="px-2 py-1 rounded text-xs font-mono font-bold bg-zinc-800 text-zinc-400">WAITING</div></div><div class="flex-1 relative w-full h-full overflow-hidden"><canvas id="main-chart"></canvas></div></div><div id="empty-state" class="w-full h-full flex flex-col items-center justify-center text-zinc-600"><i data-lucide="bar-chart-2" class="w-16 h-16 mb-4 opacity-20"></i><span>Select a stream from the left</span></div></div></div><div id="view-config" class="tab-content w-full p-8 bg-[#0c0c0e] overflow-y-auto"><div class="max-w-6xl mx-auto glass p-6 rounded-lg"><h2 class="text-xl font-bold mb-4">Device Config</h2><div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6"><div class="p-4 border border-zinc-700 rounded-lg bg-zinc-900/50"><h3 class="text-cyan-400 font-bold mb-3 flex items-center gap-2"><i data-lucide="network" class="w-4 h-4"></i> OSPF</h3><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Target Router</label><select id="config-target-ospf" class="config-target-selector w-full bg-black border border-zinc-700 p-2 rounded text-white mb-4 text-xs" onchange="syncTargetSelectors(this.value)"></select><input id="ospf-rid" placeholder="Router ID" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><input id="ospf-iface" placeholder="Interface" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><input id="ospf-area" placeholder="Area" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><button onclick="pushOSPF()" class="bg-cyan-600 hover:bg-cyan-500 text-white px-4 py-2 rounded text-xs w-full mt-2 font-bold transition">PUSH OSPF</button></div><div class="p-4 border border-zinc-700 rounded-lg bg-zinc-900/50"><h3 class="text-purple-400 font-bold mb-3 flex items-center gap-2"><i data-lucide="ethernet-port" class="w-4 h-4"></i> Interface</h3><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Target Router</label><select id="config-target-iface" class="config-target-selector w-full bg-black border border-zinc-700 p-2 rounded text-white mb-4 text-xs" onchange="syncTargetSelectors(this.value)"></select><input id="if-name" placeholder="Name (e.g. ens3)" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><input id="if-ip" placeholder="IP (e.g. 10.0.0.1/24)" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><button onclick="pushInterface()" class="bg-purple-600 hover:bg-purple-500 text-white px-4 py-2 rounded text-xs w-full mt-2 font-bold transition">PUSH IP</button></div><div class="p-4 border border-zinc-700 rounded-lg bg-zinc-900/50"><h3 class="text-emerald-400 font-bold mb-3 flex items-center gap-2"><i data-lucide="activity" class="w-4 h-4"></i> IPFIX Export</h3><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Target Router</label><select id="config-target-ipfix" class="config-target-selector w-full bg-black border border-zinc-700 p-2 rounded text-white mb-4 text-xs" onchange="syncTargetSelectors(this.value)"></select><input id="ipfix-coll" placeholder="Collector IP" class="w-full bg-black p-2 mb-2 border border-zinc-800 rounded text-white text-xs"><div class="flex gap-2 mb-2"><input id="ipfix-port" placeholder="Port (2055)" class="w-20 bg-black p-2 border border-zinc-800 rounded text-white text-xs"><input id="ipfix-iface" placeholder="Interface" class="flex-1 bg-black p-2 border border-zinc-800 rounded text-white text-xs"></div><button onclick="pushIPFIX()" class="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded text-xs w-full mt-2 font-bold transition">ENABLE FLOWS</button></div></div><div class="flex gap-4 items-end bg-zinc-900/30 p-4 rounded border border-zinc-800"><div class="flex-1"><label class="text-[10px] text-zinc-500 uppercase font-bold mb-1 block">Manual Target Override</label><input id="manual-ip" class="w-full bg-black border border-zinc-700 p-2 rounded text-white text-xs font-mono" placeholder="192.168.x.x"></div><div class="flex-1"><div id="config-status" class="text-xs font-mono text-zinc-500 h-8 overflow-y-auto pt-2">Ready.</div></div></div></div></div><div id="view-access" class="tab-content w-full h-full bg-[#0c0c0e] p-8 flex-col"><div class="max-w-5xl mx-auto w-full"><div class="flex justify-between items-end mb-6"><div><h2 class="text-2xl font-bold text-white mb-1">Approval Queue</h2><p class="text-zinc-500 text-xs">External clients requesting telemetry streams</p></div><div class="text-xs bg-zinc-900 border border-zinc-800 px-3 py-1 rounded text-zinc-400">Status: <span class="text-yellow-400">ACTIVE</span></div></div><div class="glass rounded-xl overflow-hidden overflow-x-auto"><table class="w-full text-left border-collapse"><thead class="bg-zinc-900/80 text-[10px] text-zinc-500 uppercase font-bold"><tr><th class="px-4 py-3 whitespace-nowrap">Time</th><th class="px-4 py-3 whitespace-nowrap">Client ID</th><th class="px-4 py-3 whitespace-nowrap">Target Router</th><th class="px-4 py-3 whitespace-nowrap">Sensor Path</th><th class="px-4 py-3 whitespace-nowrap">Duration</th><th class="px-4 py-3 whitespace-nowrap">Status</th><th class="px-4 py-3 text-right whitespace-nowrap">Action</th></tr></thead><tbody id="access-table" class="divide-y divide-zinc-800/50"></tbody></table></div></div></div></div><script>const ROUTER_SVG='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="#09090b" stroke="#0ea5e9" stroke-width="2"><rect x="2" y="2" width="20" height="20" rx="5"/></svg>`);let inventory={};let network=null;let selectedStreamId=null;let mainChart=null;function switchTab(t){document.querySelectorAll('.tab-content').forEach(e=>e.classList.remove('active'));document.querySelectorAll('.tab-btn').forEach(e=>e.classList.remove('active'));document.getElementById('view-'+t).classList.add('active');const btn=document.getElementById('tab-'+t);btn.classList.add('active');if(t==='topology')setTimeout(drawTopology,100)}async function toggleGlobal(){const res=await fetch('/api/flows/toggle_global',{method:'POST'});update()}async function toggleExporter(key){await fetch('/api/flows/toggle_exporter',{method:'POST',body:JSON.stringify({key})});update()}async function update(){try{const res=await fetch('/api/data');const data=await res.json();inventory=data.inventory;document.getElementById('stat-pkts').innerText=data.flow_stats.packets;updateListIfChanged('exporter-list',data.exporters,renderExporters);updateListIfChanged('flow-table',data.flows,renderFlows);updateListIfChanged('stream-list',Object.keys(data.sensors).map(k=>data.sensors[k]),renderSensorView);renderActiveDevices(data.inventory);if(selectedStreamId&&data.sensors[selectedStreamId])updateMainChart(data.sensors[selectedStreamId]);const accessRes=await fetch('/api/access/list');const accessData=await accessRes.json();updateListIfChanged('access-table',Object.values(accessData),renderAccessTable);updateDropdown('config-target-ospf',data.inventory);updateDropdown('config-target-iface',data.inventory);updateDropdown('config-target-ipfix',data.inventory);updateDropdown('sub-target',data.inventory)}catch(e){console.error("Update Loop Error:",e)}}const htmlCache={};function updateListIfChanged(elementId,data,renderFn){const tempDiv=document.createElement('div');renderFn(data,tempDiv);const newHtml=tempDiv.innerHTML;if(htmlCache[elementId]!==newHtml){document.getElementById(elementId).innerHTML=newHtml;htmlCache[elementId]=newHtml;lucide.createIcons()}}function renderAccessTable(requests,container){const t=container||document.getElementById('access-table');const sorted=requests.sort((a,b)=>(a.status==='PENDING'?-1:1));let html='';sorted.forEach(r=>{let statusBadge=r.status==='PENDING'?'<span class="bg-yellow-500/10 text-yellow-500 border border-yellow-500/20 px-2 py-1 rounded text-xs font-bold animate-pulse">PENDING</span>':r.status==='APPROVED'?'<span class="text-green-500 font-bold text-xs">APPROVED</span>':r.status==='REVOKED'?'<span class="text-orange-500 font-bold text-xs">REVOKED</span>':'<span class="text-red-500 font-bold text-xs">DENIED</span>';let actions='';if(r.status==='PENDING'){actions=`<button onclick="decide('${r.id}','APPROVE')" class="bg-green-600 hover:bg-green-500 text-white px-3 py-1 rounded text-xs font-bold mr-2">APPROVE</button><button onclick="decide('${r.id}','DENY')" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-400 px-3 py-1 rounded text-xs">DENY</button>`}else if(r.status==='APPROVED'){actions=`<button onclick="revoke('${r.id}')" class="bg-red-900/50 hover:bg-red-900 border border-red-900 text-red-300 px-3 py-1 rounded text-xs">REVOKE</button>`}else{actions='<span class="text-zinc-600 text-xs">--</span>'}html+=`<tr class="hover:bg-zinc-900/30 transition border-b border-zinc-800/50"><td class="px-4 py-3 text-zinc-500 font-mono text-xs whitespace-nowrap">${r.timestamp}</td><td class="px-4 py-3 font-bold text-white text-xs whitespace-nowrap">${r.client}</td><td class="px-4 py-3 text-zinc-400 font-mono text-xs whitespace-nowrap">${r.target_host}</td><td class="px-4 py-3 text-cyan-400 font-mono text-xs whitespace-nowrap">${r.xpath}</td><td class="px-4 py-3 text-zinc-500 font-mono text-xs whitespace-nowrap">${r.duration}s</td><td class="px-4 py-3 whitespace-nowrap">${statusBadge}</td><td class="px-4 py-3 text-right whitespace-nowrap">${actions}</td></tr>`});if(container)container.innerHTML=html;else t.innerHTML=html}async function decide(id,action){await fetch('/api/access/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:id,action:action})});const accessRes=await fetch('/api/access/list');const accessData=await accessRes.json();updateListIfChanged('access-table',Object.values(accessData),renderAccessTable)}async function revoke(id){await fetch('/api/access/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:id})});const accessRes=await fetch('/api/access/list');const accessData=await accessRes.json();updateListIfChanged('access-table',Object.values(accessData),renderAccessTable)}function renderActiveDevices(inv){const list=document.getElementById('topology-device-list');document.getElementById('device-count').innerText=Object.keys(inv).length;let html='';Object.keys(inv).forEach(h=>{html+=`<div class="p-2 rounded bg-zinc-900/50 border border-zinc-800 flex justify-between items-center hover:bg-zinc-800 transition"><div><div class="text-[10px] font-bold text-white">${h}</div><div class="text-[8px] font-mono text-zinc-500">${inv[h].meta?.source_ip||'?.?'}</div></div><div class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></div></div>`});if(list.innerHTML!==html)list.innerHTML=html}function renderSensorView(streams,container){let html='';if(streams.length===0)html='<div class="text-zinc-600 italic text-xs p-2">No active streams.</div>';else{streams.forEach(stream=>{const isSelected=(stream.id===selectedStreamId);const statusColor=stream.status==='Streaming'?'text-green-400':'text-yellow-400';html+=`<div class="p-3 rounded border cursor-pointer transition-all ${isSelected?'bg-zinc-800 border-cyan-500/50':'bg-zinc-900/50 border-zinc-800 hover:border-zinc-700'}" onclick="selectStream('${stream.id}')"><div class="flex justify-between items-start mb-1"><div class="font-bold text-xs text-white truncate w-32">${stream.path}</div><div class="text-[9px] font-mono ${statusColor}">${stream.status}</div></div><div class="flex justify-between items-center text-[10px] text-zinc-500"><div class="font-mono">${stream.id.split('-')[0]}</div><div class="font-mono text-white bg-zinc-800 px-1 rounded">${stream.values.length>0?stream.values[stream.values.length-1].y:'-'}</div></div></div>`})}if(container)container.innerHTML=html}function renderExporters(list,container){let html='';list.forEach(e=>{html+=`<div class="p-3 rounded border cursor-pointer transition-all hover:bg-zinc-800 group ${e.active?'border-emerald-500/30 bg-emerald-500/5':'border-zinc-800 bg-zinc-900/50 opacity-60'}"><div class="flex justify-between items-start mb-2"><div class="flex items-center gap-2"><div class="w-1.5 h-1.5 rounded-full ${e.active?'bg-emerald-500 animate-pulse':'bg-zinc-600'}"></div><div class="text-xs font-bold text-white">${e.hostname}</div></div><div onclick="toggleExporter('${e.key}')" class="text-[9px] px-1.5 py-0.5 rounded border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-zinc-400 transition-colors">${e.active?'MUTE':'UNMUTE'}</div></div><div class="flex justify-between items-end"><div class="text-[9px] text-zinc-500 font-mono">${e.ip}</div><div class="text-[9px] font-mono text-zinc-400">${e.packets} pkts</div></div></div>`});if(container)container.innerHTML=html}function renderFlows(flows,container){let html=flows.map(f=>`<tr class="hover:bg-white/5 border-b border-white/5 transition-colors group"><td class="px-6 py-2 text-zinc-500 font-mono group-hover:text-zinc-300 transition-colors">${f.ts}</td><td class="px-6 py-2 text-cyan-500 font-bold">${f.exporter}</td><td class="px-6 py-2 text-zinc-300 font-mono">${f.src}</td><td class="px-6 py-2 text-zinc-300 font-mono">${f.dst}</td><td class="px-6 py-2"><span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${f.app=='WEB'?'bg-blue-500/20 text-blue-400 border border-blue-500/30':f.app=='SSH'?'bg-orange-500/20 text-orange-400 border border-orange-500/30':'bg-zinc-800 text-zinc-400 border border-zinc-700'}">${f.app}</span></td><td class="px-6 py-2 text-right text-zinc-400 font-mono">${f.bytes}</td></tr>`).join('');if(container)container.innerHTML=html}function selectStream(id){selectedStreamId=id;document.getElementById('empty-state').classList.add('hidden');document.getElementById('empty-state').classList.remove('flex');document.getElementById('chart-wrapper').classList.remove('hidden');document.getElementById('chart-wrapper').classList.add('flex');fetch('/api/data').then(r=>r.json()).then(d=>updateListIfChanged('stream-list',Object.values(d.sensors),renderSensorView))}function updateMainChart(stream){document.getElementById('main-chart-title').innerText=stream.path;document.getElementById('main-chart-subtitle').innerText=`Source: ${stream.id}`;const badge=document.getElementById('main-chart-status-badge');badge.innerText=stream.status.toUpperCase();const ctx=document.getElementById('main-chart').getContext('2d');if(!mainChart)mainChart=new Chart(ctx,{type:'line',data:{labels:[],datasets:[{label:'Value',data:[],borderColor:'#22d3ee',backgroundColor:'rgba(34, 211, 238, 0.1)',borderWidth:2,pointRadius:2,fill:true,tension:0.3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:true,grid:{color:'#334155'},ticks:{color:'#64748b'}},y:{display:true,grid:{color:'#334155'},ticks:{color:'#94a3b8'}}},animation:false}});mainChart.data.labels=stream.values.map(v=>v.t);mainChart.data.datasets[0].data=stream.values.map(v=>v.y);mainChart.update()}function updateDropdown(id,inv){const sel=document.getElementById(id);if(!sel)return;if(sel.options.length<=1){for(const[h,i]of Object.entries(inv)){const opt=new Option(h,h);if(i.meta&&i.meta.source_ip){opt.setAttribute('data-ip',i.meta.source_ip)}sel.add(opt)}}}function syncTargetSelectors(value){document.querySelectorAll('.config-target-selector').forEach(sel=>{sel.value=value});const sel=document.getElementById('config-target-ospf');const opt=sel.options[sel.selectedIndex];if(opt&&opt.getAttribute('data-ip'))document.getElementById('manual-ip').value=opt.getAttribute('data-ip')}

function drawTopology() {
    const nodes = []; const edges = []; const added = new Set();
    // IMPROVED: Added 'shapeProperties' for cleaner circle outline, reduced wobbly effect
    const add = (id) => { 
        if(!added.has(id)){ 
            nodes.push({
                id, 
                label: id, 
                image: ROUTER_SVG, 
                shape: 'circularImage', // Changed to circularImage to allow background color
                size: 30, 
                font: { color: '#e4e4e7', face: 'monospace', background: 'rgba(0,0,0,0.7)', strokeWidth: 0 },
                color: { background: '#09090b', border: '#22d3ee', highlight: { border: '#38bdf8', background: '#18181b' } },
                borderWidth: 2
            }); 
            added.add(id); 
        }
    };
    
    const subnetMap = {};
    for(const [h, i] of Object.entries(inventory)) {
        add(h);
        const routes = i.network?.routes || {};
        for(const [p, rList] of Object.entries(routes)) {
            // IGNORE MANAGEMENT & LOOPBACK
            if(p === "10.140.125.0/24") continue;
            
            const arr = Array.isArray(rList) ? rList : [rList];
            // Filter connections (excluding 'ens3' as requested)
            const conn = arr.find(r => (r.protocol === "connected" || r.protocol === "kernel") && r.interface !== "ens3");
            if(conn) {
                let ifaceName = conn.interfaceName;
                if (!ifaceName && conn.nexthops) {
                     const hop = conn.nexthops.find(n => n.directlyConnected);
                     if (hop) ifaceName = hop.interfaceName;
                }
                
                // Extra check: only add if iface is NOT ens3
                if(ifaceName && ifaceName !== "ens3") {
                    if(!subnetMap[p]) subnetMap[p] = [];
                    subnetMap[p].push({host: h, iface: ifaceName});
                }
            }
        }
    }
    for(const [p, list] of Object.entries(subnetMap)) {
        if(list.length < 2) continue;
        for(let i=0; i<list.length; i++) {
            for(let j=i+1; j<list.length; j++) {
                edges.push({
                    from: list[i].host, 
                    to: list[j].host, 
                    color: { color: '#22c55e', highlight: '#4ade80' }, 
                    width: 3, // Thicker edges
                    label: `${list[i].iface} <--> ${list[j].iface}`, 
                    font: { align: 'middle', color: '#9ca3af', size: 10, background: 'rgba(0,0,0,0.8)', strokeWidth: 0 },
                    smooth: { type: 'continuous' } // Smooth curves instead of straight lines
                });
            }
        }
    }
    
    const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    
    if(network) network.destroy();
    
    // IMPROVED: Physics settings for stability ("No Wobble")
    const options = {
        physics: {
            stabilization: { enabled: true, iterations: 200 },
            barnesHut: { gravitationalConstant: -3000, springConstant: 0.04, springLength: 150, damping: 0.5 }, // High damping reduces wobble
            adaptiveTimestep: true
        },
        interaction: { hover: true, tooltipDelay: 200 },
        layout: { randomSeed: 2 } // Keep layout consistent between reloads
    };
    
    network = new vis.Network(document.getElementById('network-graph'), data, options);
}

lucide.createIcons();
setInterval(update, 1000);
update();
async function triggerSubscription(){const host=document.getElementById('sub-target').value;if(!host)return alert("Select a Target Router");const res=await fetch('/api/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hostname:host,xpath:document.getElementById('sub-path').value,period:parseInt(document.getElementById('sub-period').value),duration:parseInt(document.getElementById('sub-dur').value)})});if(res.ok)update();else alert("Failed: "+(await res.json()).message)}async function fetchCapabilities(host){const sel=document.getElementById('sub-path');sel.innerHTML='<option>Loading...</option>';try{const res=await fetch('/api/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:host})});const data=await res.json();sel.innerHTML='';data.sensors.forEach(s=>{sel.add(new Option(s,s))})}catch(e){sel.innerHTML='<option>Probe Failed</option>'}}async function pushOSPF(){await fetch('/api/push_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_ip:document.getElementById('manual-ip').value,config_type:'ospf',payload:{router_id:document.getElementById('ospf-rid').value,interfaces:[{name:document.getElementById('ospf-iface').value,area:document.getElementById('ospf-area').value}]}})});alert('Sent')}async function pushInterface(){await fetch('/api/push_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_ip:document.getElementById('manual-ip').value,config_type:'interface',payload:{name:document.getElementById('if-name').value,ip:document.getElementById('if-ip').value}})});alert('Sent')}async function pushIPFIX(){await fetch('/api/push_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_ip:document.getElementById('manual-ip').value,config_type:'ipfix',payload:{collector:document.getElementById('ipfix-coll').value,port:document.getElementById('ipfix-port').value,interfaces:[document.getElementById('ipfix-iface').value]}})});alert('Sent')}</script></body></html>
"""

@app.get("/")
def serve_ui():
    return HTMLResponse(content=HTML_UI)

@app.get("/client")
def serve_client_ui():
    return FileResponse("client_dashboard.html")

if __name__ == "__main__":
    print(f"🚀 ZAT CONTROLLER | http://{WEB_HOST}:{WEB_PORT}")
    print(f"🔗 CLIENT LINK    | http://{WEB_HOST}:{WEB_PORT}/client")
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)