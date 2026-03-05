import customtkinter as ctk
from tkinter import messagebox
import threading
import asyncio
from datetime import datetime
import sqlite3
import json
from dataclasses import dataclass, asdict
from typing import Dict, List
import queue
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

@dataclass
class CameraState:
    ip: str
    name: str
    location: str
    agent_id: str
    agent_name: str
    status: str = "UNKNOWN"
    last_ping_ms: float = None
    last_http_status: int = None
    last_seen: str = None

class Database:
    def __init__(self, db_path="server.db"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Агенты
        c.execute('''CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            agent_name TEXT,
            location TEXT,
            last_seen TEXT,
            camera_count INTEGER DEFAULT 0
        )''')
        
        # Камеры (с привязкой к агенту)
        c.execute('''CREATE TABLE IF NOT EXISTS cameras (
            ip TEXT PRIMARY KEY,
            name TEXT,
            location TEXT,
            agent_id TEXT,
            status TEXT DEFAULT 'UNKNOWN',
            last_ping_ms REAL,
            last_http_status INTEGER,
            last_seen TEXT,
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        )''')
        
        # Алерты
        c.execute('''CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            agent_name TEXT,
            camera_ip TEXT,
            camera_name TEXT,
            severity TEXT,
            transition TEXT,
            timestamp TEXT,
            acknowledged INTEGER DEFAULT 0
        )''')
        
        conn.commit()
        conn.close()
    
    def register_agent(self, agent_id: str, agent_name: str, location: str, 
                      cameras: List[dict]):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Агент
        c.execute('''INSERT OR REPLACE INTO agents 
            (agent_id, agent_name, location, last_seen, camera_count)
            VALUES (?, ?, ?, ?, ?)''',
            (agent_id, agent_name, location, datetime.utcnow().isoformat(), 
             len(cameras)))
        
        # Камеры агента
        for cam in cameras:
            c.execute('''INSERT OR REPLACE INTO cameras 
                (ip, name, location, agent_id, status)
                VALUES (?, ?, ?, ?, ?)''',
                (cam['ip'], cam['name'], cam.get('location', ''), 
                 agent_id, 'UNKNOWN'))
        
        conn.commit()
        conn.close()
    
    def update_camera(self, agent_id: str, camera_ip: str, status: str,
                     ping_ms: float = None, http_status: int = None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''UPDATE cameras SET 
            status = ?, last_ping_ms = ?, last_http_status = ?, 
            last_seen = ? WHERE ip = ? AND agent_id = ?''',
            (status, ping_ms, http_status, datetime.utcnow().isoformat(),
             camera_ip, agent_id))
        
        conn.commit()
        conn.close()
    
    def add_alert(self, alert_data: dict):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''INSERT INTO alerts 
            (agent_id, agent_name, camera_ip, camera_name, severity, 
             transition, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (alert_data['agent_id'], alert_data['agent_name'],
             alert_data['camera_ip'], alert_data['camera_name'],
             alert_data['severity'], alert_data['transition'],
             alert_data['timestamp']))
        
        conn.commit()
        alert_id = c.lastrowid
        conn.close()
        return alert_id
    
    def get_agents(self) -> List[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM agents ORDER BY agent_name")
        agents = [dict(row) for row in c.fetchall()]
        conn.close()
        return agents
    
    def get_cameras(self, agent_id: str = None) -> List[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        if agent_id:
            c.execute('''SELECT c.*, a.agent_name 
                FROM cameras c JOIN agents a ON c.agent_id = a.agent_id 
                WHERE c.agent_id = ? ORDER BY c.name''', (agent_id,))
        else:
            c.execute('''SELECT c.*, a.agent_name 
                FROM cameras c JOIN agents a ON c.agent_id = a.agent_id 
                ORDER BY a.agent_name, c.name''')
        
        cameras = [dict(row) for row in c.fetchall()]
        conn.close()
        return cameras
    
    def get_stats(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM agents")
        agents = c.fetchone()[0]
        
        c.execute("SELECT status, COUNT(*) FROM cameras GROUP BY status")
        status_counts = dict(c.fetchall())
        
        conn.close()
        
        return {
            'agents': agents,
            'online': status_counts.get('ONLINE', 0),
            'offline': status_counts.get('OFFLINE', 0),
            'degraded': status_counts.get('DEGRADED', 0),
            'vpn_issue': status_counts.get('VPN_ISSUE', 0),
            'unknown': status_counts.get('UNKNOWN', 0),
            'total': sum(status_counts.values())
        }

class APIServer:
    def __init__(self, db: Database, gui_queue: queue.Queue):
        self.db = db
        self.gui_queue = gui_queue
        self.app = FastAPI()
        self.connections: List[WebSocket] = []
        
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        self.setup_routes()
    
    def setup_routes(self):
        @self.app.post("/agent/register")
        async def register_agent(data: dict):
            self.db.register_agent(
                data['agent_id'],
                data['agent_name'],
                data['location'],
                data['cameras']
            )
            self.gui_queue.put({
                'type': 'agent_registered',
                'data': data
            })
            return {"status": "registered"}
        
        @self.app.post("/alert")
        async def receive_alert(alert: dict):
            self.db.add_alert(alert)
            self.db.update_camera(
                alert['agent_id'],
                alert['camera_ip'],
                alert['transition'].split(' → ')[-1] if ' → ' in alert.get('transition', '') else 'UNKNOWN',
                alert.get('details', {}).get('ping_ms'),
                alert.get('details', {}).get('http_status')
            )
            
            self.gui_queue.put({'type': 'alert', 'data': alert})
            await self.broadcast({'type': 'alert', 'data': alert})
            return {"ok": True}
        
        @self.app.post("/heartbeat")
        async def heartbeat(data: dict):
            self.db.update_camera(
                data['agent_id'],
                data['camera_ip'],
                data['status'],
                data.get('metrics', {}).get('ping_ms'),
                data.get('metrics', {}).get('http_status')
            )
            return {"ok": True}
        
        @self.app.get("/agents")
        async def get_agents():
            return self.db.get_agents()
        
        @self.app.get("/cameras")
        async def get_cameras(agent_id: str = None):
            return self.db.get_cameras(agent_id)
        
        @self.app.get("/stats")
        async def get_stats():
            return self.db.get_stats()
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.connections.append(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.connections.remove(websocket)
    
    async def broadcast(self, message: dict):
        disconnected = []
        for conn in self.connections:
            try:
                await conn.send_json(message)
            except:
                disconnected.append(conn)
        for d in disconnected:
            if d in self.connections:
                self.connections.remove(d)
    
    def run(self, host="0.0.0.0", port=8080):
        uvicorn.run(self.app, host=host, port=port, log_level="warning")

class MonitorServerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("Camera Monitor Server")
        self.geometry("1600x1000")
        
        self.db = Database()
        self.gui_queue = queue.Queue()
        self.agents: Dict[str, dict] = {}
        self.current_agent = None
        
        self.setup_ui()
        self.load_data()
        self.start_api_server()
        self.process_queue()
    
    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Sidebar — список агентов
        self.sidebar = ctk.CTkFrame(self, width=300, corner_radius=0)
        self.sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.sidebar.grid_propagate(False)
        
        ctk.CTkLabel(
            self.sidebar,
            text="Agents",
            font=ctk.CTkFont(size=20, weight="bold")
        ).grid(row=0, column=0, padx=20, pady=(20, 10))
        
        # Статус сервера
        self.status_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.status_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        
        ctk.CTkLabel(
            self.status_frame,
            text="●",
            text_color="#22c55e",
            font=ctk.CTkFont(size=16)
        ).pack(side="left", padx=(0, 5))
        
        ctk.CTkLabel(
            self.status_frame,
            text="Server Running :8080",
            font=ctk.CTkFont(size=12)
        ).pack(side="left")
        
        # Список агентов
        self.agents_list = ctk.CTkScrollableFrame(self.sidebar, width=260)
        self.agents_list.grid(row=2, column=0, padx=20, pady=10, sticky="nsew")
        self.sidebar.grid_rowconfigure(2, weight=1)
        
        # Глобальная статистика
        self.stats_frame = ctk.CTkFrame(self.sidebar)
        self.stats_frame.grid(row=3, column=0, padx=20, pady=20, sticky="ew")
        
        self.stats_labels = {}
        for i, (label, color) in enumerate([
            ("Agents", "#3b82f6"), ("Online", "#22c55e"), 
            ("Offline", "#ef4444"), ("Issues", "#f59e0b")
        ]):
            frame = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
            frame.grid(row=i, column=0, pady=3, sticky="ew")
            
            ctk.CTkLabel(
                frame, text=label, font=ctk.CTkFont(size=11), text_color="gray"
            ).pack(side="left")
            
            self.stats_labels[label.lower()] = ctk.CTkLabel(
                frame, text="0", font=ctk.CTkFont(size=14, weight="bold"),
                text_color=color
            )
            self.stats_labels[label.lower()].pack(side="right")
        
        # Main content
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(1, weight=1)
        
        # Header с фильтрами
        self.header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.title_label = ctk.CTkLabel(
            self.header,
            text="All Cameras",
            font=ctk.CTkFont(size=24, weight="bold")
        )
        self.title_label.pack(side="left")
        
        self.filter_var = ctk.StringVar(value="all")
        ctk.CTkOptionMenu(
            self.header,
            values=["all", "online", "offline", "issues"],
            variable=self.filter_var,
            command=lambda x: self.refresh_cameras(),
            width=120
        ).pack(side="right", padx=5)
        
        # Таблица камер
        self.cameras_frame = ctk.CTkScrollableFrame(self.main_frame)
        self.cameras_frame.grid(row=1, column=0, sticky="nsew")
        
        # Алерты снизу
        self.alerts_frame = ctk.CTkFrame(self.main_frame, height=250)
        self.alerts_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.alerts_frame.grid_propagate(False)
        
        ctk.CTkLabel(
            self.alerts_frame,
            text="Recent Alerts",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=10, pady=5)
        
        self.alerts_container = ctk.CTkScrollableFrame(self.alerts_frame, height=200)
        self.alerts_container.pack(fill="both", expand=True, padx=10, pady=5)
    
    def load_data(self):
        self.refresh_agents_list()
        self.refresh_cameras()
        self.refresh_stats()
    
    def refresh_agents_list(self):
        for widget in self.agents_list.winfo_children():
            widget.destroy()
        
        agents = self.db.get_agents()
        self.agents = {a['agent_id']: a for a in agents}
        
        # Кнопка "Все агенты"
        all_btn = ctk.CTkButton(
            self.agents_list,
            text="📁 All Agents",
            command=lambda: self.select_agent(None),
            fg_color="#1f538d" if self.current_agent is None else "#2b2b2b",
            anchor="w"
        )
        all_btn.pack(fill="x", pady=2)
        
        for agent in agents:
            btn = ctk.CTkButton(
                self.agents_list,
                text=f"📷 {agent['agent_name']}\n   {agent['camera_count']} cameras",
                command=lambda id=agent['agent_id']: self.select_agent(id),
                fg_color="#1f538d" if self.current_agent == agent['agent_id'] else "#2b2b2b",
                anchor="w",
                height=50
            )
            btn.pack(fill="x", pady=2)
    
    def select_agent(self, agent_id: str = None):
        self.current_agent = agent_id
        self.refresh_agents_list()  # Обновить цвета кнопок
        
        if agent_id:
            agent = self.agents.get(agent_id, {})
            self.title_label.configure(text=f"Agent: {agent.get('agent_name', agent_id)}")
        else:
            self.title_label.configure(text="All Cameras")
        
        self.refresh_cameras()
    
    def refresh_cameras(self):
        for widget in self.cameras_frame.winfo_children():
            widget.destroy()
        
        cameras = self.db.get_cameras(self.current_agent)
        filter_val = self.filter_var.get()
        
        colors = {
            "ONLINE": "#22c55e", "OFFLINE": "#ef4444",
            "DEGRADED": "#8b5cf6", "VPN_ISSUE": "#f59e0b", "UNKNOWN": "#6b7280"
        }
        
        # Header
        header = ctk.CTkFrame(self.cameras_frame, fg_color="#2b2b2b")
        header.pack(fill="x", pady=(0, 5))
        
        headers = ["Agent", "Camera", "Location", "Status", "Ping", "HTTP", "Last Seen"]
        widths = [150, 150, 200, 100, 80, 60, 120]
        
        for h, w in zip(headers, widths):
            ctk.CTkLabel(
                header, text=h, font=ctk.CTkFont(size=12, weight="bold"), width=w
            ).pack(side="left", padx=5)
        
        # Rows
        for cam in cameras:
            if filter_val == "online" and cam['status'] != "ONLINE":
                continue
            if filter_val == "offline" and cam['status'] != "OFFLINE":
                continue
            if filter_val == "issues" and cam['status'] not in ["DEGRADED", "VPN_ISSUE"]:
                continue
            
            row = ctk.CTkFrame(self.cameras_frame)
            row.pack(fill="x", pady=1)
            
            values = [
                cam['agent_name'][:20],
                cam['name'],
                cam['location'][:25] if cam['location'] else "-",
                cam['status'],
                f"{cam['last_ping_ms']:.1f}ms" if cam['last_ping_ms'] else "-",
                str(cam['last_http_status']) if cam['last_http_status'] else "-",
                self.format_time(cam['last_seen'])
            ]
            
            for val, w, h in zip(values, widths, headers):
                color = colors.get(cam['status'], "white") if h == "Status" else "white"
                ctk.CTkLabel(
                    row, text=val, width=w, text_color=color, font=ctk.CTkFont(size=11)
                ).pack(side="left", padx=5)
    
    def refresh_stats(self):
        stats = self.db.get_stats()
        self.stats_labels["agents"].configure(text=str(stats['agents']))
        self.stats_labels["online"].configure(text=str(stats['online']))
        self.stats_labels["offline"].configure(text=str(stats['offline']))
        issues = stats['degraded'] + stats['vpn_issue'] + stats['unknown']
        self.stats_labels["issues"].configure(text=str(issues))
    
    def format_time(self, iso_string: str) -> str:
        if not iso_string:
            return "Never"
        try:
            dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
            now = datetime.utcnow()
            diff = (now - dt.replace(tzinfo=None)).total_seconds()
            
            if diff < 60:
                return "now"
            elif diff < 3600:
                return f"{int(diff/60)}m"
            elif diff < 86400:
                return f"{int(diff/3600)}h"
            else:
                return f"{int(diff/86400)}d"
        except:
            return "-"
    
    def start_api_server(self):
        self.api_server = APIServer(self.db, self.gui_queue)
        self.server_thread = threading.Thread(
            target=self.api_server.run,
            args=("0.0.0.0", 8080),
            daemon=True
        )
        self.server_thread.start()
    
    def process_queue(self):
        try:
            while True:
                msg = self.gui_queue.get_nowait()
                
                if msg['type'] == 'agent_registered':
                    self.refresh_agents_list()
                    self.refresh_stats()
                    
                elif msg['type'] == 'alert':
                    self.refresh_cameras()
                    self.refresh_stats()
                    self.add_alert_to_list(msg['data'])
                    
        except queue.Empty:
            pass
        
        self.after(100, self.process_queue)
    
    def add_alert_to_list(self, alert: dict):
        # Ограничить количество видимых алертов
        for widget in self.alerts_container.winfo_children()[:20]:
            widget.destroy()
        
        frame = ctk.CTkFrame(self.alerts_container)
        frame.pack(fill="x", pady=2)
        
        color = "#ef4444" if alert['severity'] == "CRITICAL" else "#f59e0b"
        
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(
            header,
            text=f"[{alert['agent_name']}] {alert['camera_name']}",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left")
        
        ctk.CTkLabel(
            header,
            text=self.format_time(alert['timestamp']),
            font=ctk.CTkFont(size=10),
            text_color="gray"
        ).pack(side="right")
        
        ctk.CTkLabel(
            frame,
            text=alert['transition'],
            font=ctk.CTkFont(size=11),
            text_color=color
        ).pack(anchor="w", padx=10)

if __name__ == "__main__":
    app = MonitorServerApp()
    app.mainloop()

    