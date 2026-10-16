"""
collector_tray.py
即時機況表 - 背景狀態變化採集工具（常駐系統匣版本）

用途：
  雙擊執行後，先跳出一個小視窗，讓使用者指定：
    - 來源資料夾（存放 HandlerStatus_<機台>.csv 的母資料夾）
    - 輸出資料夾（state_history.csv 要寫到哪裡，可以跟來源資料夾不同）
  確認後，程式常駐在背景（右下角工作列會出現一個圖示），
  內部每 60 秒自動採集一次：讀取來源資料夾下所有 HandlerStatus_<機台>.csv，
  取得每台機台目前的 Handler Status，跟輸出資料夾裡 state_history.csv 的
  上一筆紀錄比對，只要狀態有變化就寫入一行新紀錄。

設定視窗：
  - 兩個路徑輸入框 + 「瀏覽...」按鈕選擇資料夾
  - 「開始執行」按鈕：驗證兩個路徑都已選擇且存在，才會開始背景採集
  - 設定值會存成 collector_settings.json（跟 exe 放在同一層），
    下次啟動會自動帶入上次的設定，不用每次重選

系統匣圖示：
  - 滑鼠移到圖示上：顯示提示文字，包含上次執行時間與本次寫入筆數
  - 右鍵選單：
      「立即執行一次」  手動觸發一次採集，不用等 60 秒
      「開啟來源資料夾」「開啟輸出資料夾」
      「結束程式」      停止背景採集並結束程式

執行方式：
  雙擊 collector_tray.exe，跳出設定視窗，選好路徑後按「開始執行」。
  想開機自動啟動，把 collector_tray.exe 的捷徑放到：
    Windows 鍵 + R，輸入 shell:startup，把捷徑丟進打開的資料夾即可
    （下次自動啟動時，若已有存好的設定，會直接使用不再詢問；
      若想重新指定路徑，可在設定視窗手動修改）。

輸出：
  輸出資料夾下的 state_history.csv，欄位：Machine, Status, StartTime, RecordedAt
"""

import csv
import glob
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

STATE_FILE_NAME = "state_history.csv"
HANDLER_STATUS_PATTERN = "HandlerStatus_*.csv"
INTERVAL_SECONDS = 60
SETTINGS_FILE_NAME = "collector_settings.json"


def get_exe_folder():
    """取得 exe（或腳本）自己所在的資料夾，用來存放/讀取設定檔。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_settings():
    """讀取上次儲存的來源/輸出資料夾設定，檔案不存在或格式錯誤時回傳空字典。"""
    path = os.path.join(get_exe_folder(), SETTINGS_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(source_folder, output_folder):
    """把使用者選擇的路徑存起來，下次啟動自動帶入。"""
    path = os.path.join(get_exe_folder(), SETTINGS_FILE_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"source_folder": source_folder, "output_folder": output_folder}, f, ensure_ascii=False)
    except Exception:
        pass


def extract_machine_from_filename(filename):
    base = os.path.basename(filename)
    m = re.match(r"^HandlerStatus_(.+)\.csv$", base, re.IGNORECASE)
    return m.group(1) if m else os.path.splitext(base)[0]


def read_current_status(csv_path):
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None
            normalized = [h.strip().lower() for h in header]
            try:
                status_idx = normalized.index("handler status")
            except ValueError:
                status_idx = 2
            for row in reader:
                if len(row) > status_idx:
                    status = row[status_idx].strip()
                    return status if status else None
            return None
    except Exception:
        return None


def load_last_known_states(state_file_path):
    last_states = {}
    if not os.path.exists(state_file_path):
        return last_states
    try:
        with open(state_file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                machine = row.get("Machine", "").strip()
                status = row.get("Status", "").strip()
                if machine:
                    last_states[machine] = status
    except Exception:
        pass
    return last_states


def append_state_changes(state_file_path, changes, now_str):
    file_exists = os.path.exists(state_file_path)
    with open(state_file_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Machine", "Status", "StartTime", "RecordedAt"])
        for machine, status in changes:
            writer.writerow([machine, status, now_str, now_str])


def run_once(source_folder, output_folder, log_callback=None):
    """執行一次採集：從 source_folder 讀取 CSV，寫入 output_folder 底下的 state_history.csv。
    回傳 (成功與否, 訊息字串, 本次寫入筆數)，供系統匣提示文字與選單使用。"""
    def log(msg):
        if log_callback:
            log_callback(msg)

    now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    csv_files = glob.glob(os.path.join(source_folder, HANDLER_STATUS_PATTERN))
    if not csv_files:
        msg = f"找不到 {HANDLER_STATUS_PATTERN} 檔案"
        log(f"[{now_str}] {msg}")
        return False, msg, 0

    state_file_path = os.path.join(output_folder, STATE_FILE_NAME)
    last_states = load_last_known_states(state_file_path)

    changes = []
    for csv_path in sorted(csv_files):
        machine = extract_machine_from_filename(csv_path)
        current_status = read_current_status(csv_path)
        if current_status is None:
            continue
        previous_status = last_states.get(machine)
        if previous_status != current_status:
            changes.append((machine, current_status))

    if changes:
        append_state_changes(state_file_path, changes, now_str)
        msg = f"寫入 {len(changes)} 筆變化"
    else:
        msg = "無狀態變化"

    log(f"[{now_str}] {msg}")
    return True, msg, len(changes)


class CollectorService:
    """背景常駐服務：管理計時器執行緒與最新狀態，供系統匣圖示查詢顯示。"""

    def __init__(self, source_folder, output_folder):
        self.source_folder = source_folder
        self.output_folder = output_folder
        self.last_run_time = None
        self.last_message = "尚未執行"
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def _loop(self):
        while self.running:
            self.run_now()
            # 用短間隔輪詢代替一次長 sleep，讓「結束程式」能更快反應，不用等到整個 60 秒跑完
            waited = 0
            while waited < INTERVAL_SECONDS and self.running:
                time.sleep(1)
                waited += 1

    def run_now(self):
        ok, msg, count = run_once(self.source_folder, self.output_folder)
        self.last_run_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.last_message = msg
        return ok, msg, count

    def stop(self):
        self.running = False

    def tooltip_text(self):
        if self.last_run_time is None:
            return "HandlerStatus Collector\n尚未執行第一次採集"
        return f"HandlerStatus Collector\n上次執行：{self.last_run_time}\n{self.last_message}"


def build_tray_icon_image():
    """用 Pillow 畫一個簡單的圓形圖示（避免額外依賴圖檔資源，打包時不用另外附圖片檔）。"""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(15, 110, 86, 255))  # 跟網頁工具主題色一致的墨綠色
    draw.text((size // 2 - 10, size // 2 - 14), "HS", fill=(255, 255, 255, 255))
    return img


def show_settings_window():
    """跳出設定視窗，讓使用者選擇來源資料夾與輸出資料夾。
    回傳 (source_folder, output_folder)；使用者按「取消」或直接關閉視窗則回傳 (None, None)。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    saved = load_settings()
    result = {"source": None, "output": None}

    root = tk.Tk()
    root.title("HandlerStatus Collector - 設定")
    root.geometry("520x220")
    root.resizable(False, False)

    tk.Label(root, text="即時機況表 - 背景狀態採集設定", font=("Microsoft JhengHei", 12, "bold")).pack(pady=(16, 12))

    # 來源資料夾
    frame1 = tk.Frame(root)
    frame1.pack(fill="x", padx=20, pady=6)
    tk.Label(frame1, text="來源資料夾（HandlerStatus_*.csv 所在位置）", anchor="w").pack(fill="x")
    row1 = tk.Frame(frame1)
    row1.pack(fill="x", pady=(2, 0))
    source_var = tk.StringVar(value=saved.get("source_folder", ""))
    tk.Entry(row1, textvariable=source_var).pack(side="left", fill="x", expand=True)

    def browse_source():
        path = filedialog.askdirectory(title="選擇來源資料夾（HandlerStatus_*.csv 所在位置）")
        if path:
            source_var.set(path)

    tk.Button(row1, text="瀏覽...", command=browse_source).pack(side="left", padx=(6, 0))

    # 輸出資料夾
    frame2 = tk.Frame(root)
    frame2.pack(fill="x", padx=20, pady=6)
    tk.Label(frame2, text="輸出資料夾（state_history.csv 要寫到哪裡）", anchor="w").pack(fill="x")
    row2 = tk.Frame(frame2)
    row2.pack(fill="x", pady=(2, 0))
    output_var = tk.StringVar(value=saved.get("output_folder", ""))
    tk.Entry(row2, textvariable=output_var).pack(side="left", fill="x", expand=True)

    def browse_output():
        path = filedialog.askdirectory(title="選擇輸出資料夾（state_history.csv 要寫到哪裡）")
        if path:
            output_var.set(path)

    tk.Button(row2, text="瀏覽...", command=browse_output).pack(side="left", padx=(6, 0))

    def on_start():
        source = source_var.get().strip()
        output = output_var.get().strip()
        if not source or not os.path.isdir(source):
            messagebox.showerror("設定錯誤", "請選擇有效的來源資料夾")
            return
        if not output or not os.path.isdir(output):
            messagebox.showerror("設定錯誤", "請選擇有效的輸出資料夾")
            return
        result["source"] = source
        result["output"] = output
        save_settings(source, output)
        root.destroy()

    def on_cancel():
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(20, 0))
    tk.Button(btn_frame, text="開始執行", width=14, command=on_start).pack(side="left", padx=6)
    tk.Button(btn_frame, text="取消", width=14, command=on_cancel).pack(side="left", padx=6)

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()

    return result["source"], result["output"]


def main():
    source_folder, output_folder = show_settings_window()
    if not source_folder or not output_folder:
        # 使用者取消設定，直接結束程式
        return

    service = CollectorService(source_folder, output_folder)
    service.start()

    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError:
        print("[錯誤] 缺少 pystray 套件，無法顯示系統匣圖示。")
        print("此為打包環境問題，請確認 requirements.txt 有包含 pystray 與 pillow。")
        sys.exit(1)

    def on_run_now(icon, item):
        service.run_now()
        icon.title = service.tooltip_text()

    def on_open_source_folder(icon, item):
        try:
            os.startfile(source_folder)  # Windows 專用，開啟檔案總管
        except Exception:
            pass

    def on_open_output_folder(icon, item):
        try:
            os.startfile(output_folder)
        except Exception:
            pass

    def on_quit(icon, item):
        service.stop()
        icon.stop()

    icon_image = build_tray_icon_image()
    tray_icon = pystray.Icon(
        "handler_status_collector",
        icon_image,
        service.tooltip_text(),
        menu=pystray.Menu(
            Item("立即執行一次", on_run_now),
            Item("開啟來源資料夾", on_open_source_folder),
            Item("開啟輸出資料夾", on_open_output_folder),
            pystray.Menu.SEPARATOR,
            Item("結束程式", on_quit),
        ),
    )

    # 每秒更新一次滑鼠提示文字，讓「上次執行時間」保持最新（pystray 本身不會自動輪詢，要手動更新 title）
    def refresh_tooltip():
        while service.running:
            tray_icon.title = service.tooltip_text()
            time.sleep(1)

    threading.Thread(target=refresh_tooltip, daemon=True).start()

    tray_icon.run()  # 阻塞在這裡，直到使用者選「結束程式」


if __name__ == "__main__":
    main()
