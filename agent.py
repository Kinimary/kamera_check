import asyncio
import aiohttp
import aioping
import json
import sys
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional

# GUI для трея
import tkinter as tk
from tkinter import messagebox, filedialog
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

@dataclass
class Camera:
    ip: str
    name: str
    location: str = ""
    web_port: int = 80
    web_path: str = "/"

class AgentCore:
    """Ядро мониторинга — без GUI"""
    def __init__(self, config_path: str, server_url: str, status_callback=None):
        self.config_path = config_path
        self.server_url = server_url
        self.status_callback = status_callback  # Для обновления GUI
        
        self.config = {}
        self.cameras: List[Camera] = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        
        self.check_interval = 30
        self.ping_timeout = 2.0
        self.alert_cooldown = 300
        
        self.last_status: Dict[str, str] = {}
        self.last_alert_time: Dict[str, datetime] = {}
        self.stats = {"online": 0, "offline": 0, "total": 0}
        
        self.load_config()
    
    def load_config(self):
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.config = data['agent']
            for item in data['cameras']:
                self.cameras.append(Camera(**item))
    
    async def start(self):
        self.running = True
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, ssl=False),
            timeout=aiohttp.ClientTimeout(total=10)
        )
        
        await self.register()
        
        await asyncio.gather(
            self.monitor_loop(),
            self.daily_reporter()
        )
    
    async def register(self):
        payload = {
            'agent_id': self.config['id'],
            'agent_name': self.config['name'],
            'location': self.config['location'],
            'cameras': [
                {'ip': c.ip, 'name': c.name, 'location': c.location,
                 'web_port': c.web_port, 'web_path': c.web_path}
                for c in self.cameras
            ],
            'timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            async with self.session.post(
                f'{self.server_url}/agent/register',
                json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                self.log(f"Registered: {resp.status}")
        except Exception as e:
            self.log(f"Register failed: {e}")
    
    async def check_camera(self, cam: Camera) -> dict:
        result = {'ping_ms': None, 'http_status': None, 'vpn_ok': True}
        
        try:
            delay = await aioping.ping(cam.ip, timeout=self.ping_timeout)
            result['ping_ms'] = delay * 1000
        except:
            pass
        
        if result['ping_ms']:
            try:
                url = f"http://{cam.ip}:{cam.web_port}{cam.web_path}"
                async with self.session.head(url, allow_redirects=True) as resp:
                    result['http_status'] = resp.status
            except:
                try:
                    async with self.session.get(url) as resp:
                        result['http_status'] = resp.status
                except:
                    pass
        
        vpn_target = self.config.get('vpn_target')
        if vpn_target and result['ping_ms']:
            try:
                await aioping.ping(vpn_target, timeout=2.0)
            except:
                result['vpn_ok'] = False
        
        return result
    
    def determine_status(self, result: dict) -> str:
        if result['ping_ms'] is None:
            return "OFFLINE"
        if result['http_status'] != 200:
            return "DEGRADED"
        if not result.get('vpn_ok', True):
            return "VPN_ISSUE"
        return "ONLINE"
    
    async def monitor_loop(self):
        sem = asyncio.Semaphore(50)
        
        async def check_one(cam: Camera):
            while self.running:
                async with sem:
                    result = await self.check_camera(cam)
                    new_status = self.determine_status(result)
                    old_status = self.last_status.get(cam.ip, "UNKNOWN")
                    
                    if new_status != old_status:
                        await self.send_alert(cam, old_status, new_status, result)
                        self.last_status[cam.ip] = new_status
                        self.update_stats()
                
                await asyncio.sleep(self.check_interval)
        
        await asyncio.gather(*[check_one(c) for c in self.cameras])
    
    def update_stats(self):
        self.stats["online"] = sum(1 for s in self.last_status.values() if s == "ONLINE")
        self.stats["offline"] = sum(1 for s in self.last_status.values() if s == "OFFLINE")
        self.stats["total"] = len(self.cameras)
        
        if self.status_callback:
            self.status_callback(self.stats)
    
    async def send_alert(self, cam: Camera, old_status: str, new_status: str, result: dict):
        last_alert = self.last_alert_time.get(cam.ip)
        if last_alert and (datetime.utcnow() - last_alert).seconds < self.alert_cooldown:
            if new_status != "OFFLINE":
                return
        
        alert = {
            'agent_id': self.config['id'],
            'agent_name': self.config['name'],
            'camera_ip': cam.ip,
            'camera_name': cam.name,
            'severity': 'CRITICAL' if new_status == 'OFFLINE' else 'WARNING',
            'transition': f'{old_status} → {new_status}',
            'details': result,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            async with self.session.post(
                f'{self.server_url}/alert', json=alert, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    self.last_alert_time[cam.ip] = datetime.utcnow()
                    self.log(f"{cam.name}: {alert['transition']}")
        except Exception as e:
            self.log(f"Alert failed: {e}")
    
    async def daily_reporter(self):
        while self.running:
            now = datetime.utcnow()
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            await asyncio.sleep((next_run - now).total_seconds())
            
            if not self.running:
                break
            
            report = {
                'agent_id': self.config['id'],
                'agent_name': self.config['name'],
                'stats': {
                    'total': len(self.cameras),
                    'online': self.stats["online"],
                    'offline': self.stats["offline"]
                },
                'timestamp': datetime.utcnow().isoformat()
            }
            
            try:
                await self.session.post(f'{self.server_url}/report/daily', json=report)
            except:
                pass
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{ts}] {msg}"
        print(full_msg)
        # Можно писать в файл или показывать в GUI
    
    def stop(self):
        self.running = False
        if self.session:
            asyncio.create_task(self.session.close())

class TrayApp:
    """GUI приложение в системном трее"""
    def __init__(self):
        self.agent: Optional[AgentCore] = None
        self.agent_thread: Optional[threading.Thread] = None
        self.config_path: Optional[str] = None
        self.server_url: str = "http://localhost:8080"
        
        self.window = tk.Tk()
        self.window.title("Camera Agent")
        self.window.geometry("400x300")
        self.window.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        self.setup_ui()
        
        if HAS_TRAY:
            self.setup_tray()
            self.hide_window()  # Сразу в трей
        else:
            self.window.deiconify()
        
        self.window.mainloop()
    
    def setup_ui(self):
        # Конфиг
        tk.Label(self.window, text="Config:").pack(pady=5)
        
        cfg_frame = tk.Frame(self.window)
        cfg_frame.pack(fill="x", padx=20)
        
        self.cfg_entry = tk.Entry(cfg_frame)
        self.cfg_entry.pack(side="left", fill="x", expand=True)
        
        tk.Button(cfg_frame, text="Browse", command=self.browse_config).pack(side="right", padx=5)
        
        # Сервер
        tk.Label(self.window, text="Server URL:").pack(pady=5)
        self.server_entry = tk.Entry(self.window)
        self.server_entry.insert(0, self.server_url)
        self.server_entry.pack(fill="x", padx=20)
        
        # Статус
        self.status_label = tk.Label(self.window, text="Stopped", fg="red")
        self.status_label.pack(pady=10)
        
        self.stats_label = tk.Label(self.window, text="Cameras: 0 | Online: 0 | Offline: 0")
        self.stats_label.pack()
        
        # Кнопки
        btn_frame = tk.Frame(self.window)
        btn_frame.pack(pady=20)
        
        self.start_btn = tk.Button(btn_frame, text="Start", command=self.start_agent, width=10)
        self.start_btn.pack(side="left", padx=5)
        
        tk.Button(btn_frame, text="Stop", command=self.stop_agent, width=10).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Exit", command=self.exit_app, width=10).pack(side="left", padx=5)
        
        # Лог
        self.log_text = tk.Text(self.window, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=20, pady=10)
    
    def setup_tray(self):
        # Иконка 16x16
        image = Image.new('RGB', (16, 16), color='black')
        dc = ImageDraw.Draw(image)
        dc.rectangle([0, 0, 15, 15], fill='green', outline='white')
        
        menu = pystray.Menu(
            pystray.MenuItem("Show", self.show_window),
            pystray.MenuItem("Start", self.start_agent),
            pystray.MenuItem("Stop", self.stop_agent),
            pystray.MenuItem("Exit", self.exit_app)
        )
        
        self.tray_icon = pystray.Icon("camera_agent", image, "Camera Agent", menu)
        
        # Запуск трея в отдельном потоке
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
    
    def browse_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            self.config_path = path
            self.cfg_entry.delete(0, tk.END)
            self.cfg_entry.insert(0, path)
    
    def start_agent(self):
        if not self.config_path:
            messagebox.showerror("Error", "Select config file first")
            return
        
        self.server_url = self.server_entry.get()
        
        self.agent = AgentCore(
            self.config_path, 
            self.server_url,
            status_callback=self.update_gui_stats
        )
        
        self.agent_thread = threading.Thread(
            target=lambda: asyncio.run(self.agent.start()),
            daemon=True
        )
        self.agent_thread.start()
        
        self.status_label.config(text="Running", fg="green")
        self.start_btn.config(state="disabled")
        self.log("Agent started")
    
    def stop_agent(self):
        if self.agent:
            self.agent.stop()
            self.agent = None
        
        self.status_label.config(text="Stopped", fg="red")
        self.start_btn.config(state="normal")
        self.log("Agent stopped")
    
    def update_gui_stats(self, stats: dict):
        self.window.after(0, lambda: self.stats_label.config(
            text=f"Cameras: {stats['total']} | Online: {stats['online']} | Offline: {stats['offline']}"
        ))
    
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.window.after(0, lambda: self._append_log(f"[{ts}] {msg}\n"))
    
    def _append_log(self, text: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.config(state="disabled")
    
    def hide_window(self):
        if HAS_TRAY:
            self.window.withdraw()
        else:
            self.window.iconify()
    
    def show_window(self):
        self.window.deiconify()
        self.window.lift()
    
    def exit_app(self):
        self.stop_agent()
        if HAS_TRAY and hasattr(self, 'tray_icon'):
            self.tray_icon.stop()
        self.window.destroy()
        sys.exit(0)

if __name__ == "__main__":
    # Проверка аргументов — если есть, запуск без GUI (фоновый режим)
    if len(sys.argv) >= 3:
        # Консольный режим
        agent = AgentCore(sys.argv[1], sys.argv[2])
        try:
            asyncio.run(agent.start())
        except KeyboardInterrupt:
            print("Stopped")
    else:
        # GUI режим
        if not HAS_TRAY:
            print("Install pystray: pip install pystray pillow")
        TrayApp()