#!/usr/bin/env python3
"""Control Flip Flat v8.1 — Ubuntu / INDI 1.9.9+.

Instalar: sudo apt install python3-tk indi-bin
Requiere driver indi_flipflat y herramientas indi_getprop / indi_setprop.

Las consultas se hacen sin patrón de propiedades (compatible con INDI 1.9.9),
con -t 2 y lectura parcial cuando indi_getprop no termina espontáneamente.
"""

import concurrent.futures
import datetime as dt
import json
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

HOST = "127.0.0.1"
PORT = 7624
DEVICE = "Flip Flat"
DRIVER = "indi_flipflat"
DEFAULT_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"

CONN = f"{DEVICE}.CONNECTION.CONNECT"
DISC = f"{DEVICE}.CONNECTION.DISCONNECT"
SERIAL = f"{DEVICE}.DEVICE_PORT.PORT"
ON = f"{DEVICE}.FLAT_LIGHT_CONTROL.FLAT_LIGHT_ON"
OFF = f"{DEVICE}.FLAT_LIGHT_CONTROL.FLAT_LIGHT_OFF"
BRIGHT = f"{DEVICE}.FLAT_LIGHT_INTENSITY.FLAT_LIGHT_INTENSITY_VALUE"
LIGHT = f"{DEVICE}.Status.Light"

PROFILE_FILE = Path.home() / ".config" / "control_flipflat" / "profiles.json"
FILTERS = ("L", "R", "G", "B", "Hα", "O III", "S II", "Bessel-V", "UV/IR")


class BrightnessProfiles:
    """Almacén persistente de intensidades confirmadas por INDI."""

    def __init__(self):
        self.values = {name: None for name in FILTERS}
        self.selected = "L"
        self.load()

    def load(self):
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                saved = data.get("values", {})
                if isinstance(saved, dict):
                    for name in FILTERS:
                        val = saved.get(name)
                        if type(val) is int and 0 <= val <= 255:
                            self.values[name] = val
                if data.get("selected") in FILTERS:
                    self.selected = data["selected"]
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            logger.warning("No se pudieron leer los perfiles: %s", exc)

    def save(self):
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": 1, "selected": self.selected, "values": self.values},
                ensure_ascii=False, indent=2
            ) + "\n", encoding="utf-8"
        )
        os.replace(tmp, PROFILE_FILE)

    def set_value(self, filter_name, brightness):
        self.values[filter_name] = int(brightness)
        self.save()


LOG_FILE = Path.home() / ".local" / "state" / "control_flipflat" / "control_flipflat.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("flipflat")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


class IndiError(Exception):
    pass


class Controller:
    def __init__(self):
        self.owned = None
        self.serial_port = DEFAULT_PORT
        self.cache = {}
        self.lock = threading.RLock()

    @staticmethod
    def prerequisites():
        missing = [x for x in ("indiserver", DRIVER, "indi_getprop", "indi_setprop")
                   if shutil.which(x) is None]
        if missing:
            raise IndiError("Faltan herramientas INDI: " + ", ".join(missing))

    @staticmethod
    def server_alive():
        try:
            with socket.create_connection((HOST, PORT), timeout=0.6):
                return True
        except OSError:
            return False

    def start(self):
        if self.server_alive():
            logger.info("Se reutiliza un servidor INDI externo")
            return "Servidor existente"
        self.owned = subprocess.Popen(
            ["indiserver", "-p", str(PORT), DRIVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if self.server_alive():
                logger.info("Servidor INDI iniciado PID=%s", self.owned.pid)
                return "Servidor iniciado"
            if self.owned.poll() is not None:
                rc = self.owned.returncode
                self.owned = None
                raise IndiError(f"indiserver terminó con código {rc}")
            time.sleep(0.15)
        self.stop_owned()
        raise IndiError("El servidor no abrió el puerto TCP 7624")

    def stop_owned(self):
        if self.owned is not None:
            if self.owned.poll() is None:
                self.owned.terminate()
                try:
                    self.owned.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.owned.kill()
                    self.owned.wait(timeout=2)
                logger.info("Servidor INDI propio detenido")
            self.owned = None

    @staticmethod
    def _execute(argv, timeout=5, partial=False):
        """Subprocess sin shell; admite salida parcial para indi_getprop."""
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        expired = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired = True
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=1)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if expired and not (partial and out.strip()):
            raise IndiError("Tiempo de espera de INDI: " + " ".join(argv))
        if not expired and proc.returncode != 0 and not (partial and out.strip()):
            raise IndiError(err.strip() or out.strip() or "Error: " + " ".join(argv))
        return out

    def properties(self):
        """Consulta las propiedades del dispositivo con un patrón INDI válido.

        INDI 1.9.9 exige tres componentes en los patrones. El patrón
        explícito «Flip Flat.*.*» evita el *.*.* implícito y reduce
        las respuestas ajenas al dispositivo.
        """
        if not self.server_alive():
            raise IndiError("No hay servidor INDI escuchando en 127.0.0.1:7624")

        last_error = None
        for attempt in range(3):
            try:
                out = self._execute(
                    ["indi_getprop", "-h", HOST, "-p", str(PORT),
                     "-t", "4", f"{DEVICE}.*.*"],
                    timeout=7, partial=True
                )
                parsed = {}
                for line in out.splitlines():
                    if "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    if key.startswith(DEVICE + "."):
                        parsed[key] = val.strip()
                if parsed:
                    self.cache = parsed
                    return parsed
                last_error = "La consulta no devolvió propiedades del dispositivo"
            except IndiError as exc:
                last_error = str(exc)
            if attempt < 2:
                time.sleep(0.4)
        raise IndiError(
            "No se han podido leer las propiedades del Flip Flat "
            "tras tres intentos.\n" + str(last_error)
        )

    def set(self, key, value):
        if not self.server_alive():
            raise IndiError("El servidor INDI se ha detenido")
        # No añadir comillas al argumento: subprocess no invoca un shell.
        self._execute(
            ["indi_setprop", "-h", HOST, "-p", str(PORT), f"{key}={value}"],
            timeout=6
        )
        logger.info("INDI %s=%s", key, value)

    def wait_value(self, key, expected, timeout=9):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            props = self.properties()
            last = props.get(key)
            if last == expected:
                return props
            time.sleep(0.15)
        raise IndiError(f"Sin confirmación de {key}={expected}; valor recibido: {last!r}")

    @staticmethod
    def snapshot(props):
        try:
            brightness = int(round(float(props[BRIGHT])))
        except (ValueError, KeyError, TypeError):
            brightness = None
        status = props.get(LIGHT, "").lower()
        if status == "on":
            lit = True
        elif status == "off":
            lit = False
        elif props.get(ON) == "On":
            lit = True
        elif props.get(OFF) == "On":
            lit = False
        else:
            lit = None
        return brightness, lit

    def connect(self):
        props = self.properties()
        if CONN not in props:
            raise IndiError("El driver Flip Flat no publica CONNECTION.CONNECT")
        if props[CONN] != "On":
            if SERIAL in props and self.serial_port:
                self.set(SERIAL, self.serial_port)
            self.set(CONN, "On")
            props = self.wait_value(CONN, "On", timeout=12)
        missing = [key for key in (ON, OFF, BRIGHT) if key not in props]
        if missing:
            props = self.properties()
            missing = [key for key in (ON, OFF, BRIGHT) if key not in props]
        if missing:
            raise IndiError("Conectado, pero faltan propiedades:\n" + "\n".join(missing))
        logger.info("Flip Flat conectado")
        return self.snapshot(props)

    def refresh(self):
        props = self.properties()
        if props.get(CONN) != "On":
            raise IndiError("Flip Flat desconectado")
        return self.snapshot(props)

    def set_brightness(self, value):
        value = max(0, min(255, int(value)))
        props = self.properties()
        if props.get(CONN) != "On" or BRIGHT not in props:
            raise IndiError("No está disponible el control de brillo")
        self.set(BRIGHT, value)
        props = self.wait_value(BRIGHT, str(value), timeout=8)
        return self.snapshot(props)

    def set_light(self, enabled):
        props = self.properties()
        if props.get(CONN) != "On":
            raise IndiError("El panel no está conectado")
        key = ON if enabled else OFF
        if key not in props:
            raise IndiError("No está disponible el control de iluminación")
        self.set(key, "On")
        target = "On" if enabled else "Off"
        until = time.monotonic() + 8
        while time.monotonic() < until:
            props = self.properties()
            if (props.get(LIGHT) == target or
                    (LIGHT not in props and props.get(key) == "On")):
                return self.snapshot(props)
            time.sleep(0.15)
        raise IndiError(f"No se confirmó el estado de la luz: {target}")

    def finish(self):
        """Apagar, desconectar y detener solo el servidor propio.

        Si no se puede confirmar el apagado, no se desconecta automáticamente
        para no dejar la luz encendida sin control.
        """
        props = self.properties()
        if props.get(CONN) == "On":
            self.set_light(False)
            self.set(DISC, "On")
            self.wait_value(CONN, "Off", timeout=8)
            logger.info("Panel desconectado")
        self.stop_owned()
        return "Servidor externo conservado" if self.server_alive() else "Servidor detenido"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Control Flip Flat v9.1 — perfiles de brillo")
        self.geometry("850x730")
        self.minsize(680, 620)
        self.controller = Controller()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.connected = False
        self.busy = False
        self.closing = False
        self.value = tk.DoubleVar(value=0)
        self._updating = False
        self.night_mode = tk.BooleanVar(value=False)
        self.serial_port_var = tk.StringVar(value=DEFAULT_PORT)
        self.profiles = BrightnessProfiles()
        self.filter_var = tk.StringVar(value=self.profiles.selected)
        self.profile_info = tk.StringVar(value="")
        self._style = ttk.Style(self)
        self._build()
        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.initial_check)

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, padx=20, pady=(16, 10), sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Optec / Alnitak Flip Flat — v9.1",
                  font=("Sans", 19, "bold")).grid(row=0, column=0, sticky="w")
        self.night_toggle = ttk.Checkbutton(
            header, text="Modo nocturno (rojo)", variable=self.night_mode,
            command=self.apply_theme
        )
        self.night_toggle.grid(row=0, column=1, padx=(12, 0))

        server = ttk.LabelFrame(self, text="Servidor INDI")
        server.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        server.columnconfigure(0, weight=1)
        self.server_label = ttk.Label(server, text="● Sin comprobar")
        self.server_label.grid(row=0, column=0, padx=12, pady=10, sticky="w")
        self.server_btn = ttk.Button(server, text="Iniciar servidor", command=self.start_server)
        self.server_btn.grid(row=0, column=1, padx=12, pady=10)

        device = ttk.LabelFrame(self, text="Dispositivo")
        device.grid(row=2, column=0, padx=20, pady=5, sticky="ew")
        device.columnconfigure(0, weight=1)
        self.conn_label = ttk.Label(device, text="● Desconectado")
        self.conn_label.grid(row=0, column=0, padx=12, pady=10, sticky="w")
        self.connect_btn = ttk.Button(device, text="Conectar", command=self.connect)
        self.connect_btn.grid(row=0, column=1, padx=12, pady=10)

        ttk.Label(device, text="Puerto serie:").grid(
            row=1, column=0, padx=(12, 6), pady=(0, 12), sticky="w"
        )
        self.port_entry = ttk.Entry(
            device, textvariable=self.serial_port_var, width=55
        )
        self.port_entry.grid(
            row=2, column=0, columnspan=2, padx=12, pady=(0, 12), sticky="ew"
        )

        panel = ttk.LabelFrame(self, text="Iluminación")
        panel.grid(row=3, column=0, padx=20, pady=5, sticky="nsew")
        panel.columnconfigure(1, weight=1)
        self.led = tk.Canvas(panel, width=36, height=36, highlightthickness=0)
        self.led.grid(row=0, column=0, padx=12, pady=10)
        self.dot = self.led.create_oval(5, 5, 30, 30, fill="#888888")
        self.light_label = ttk.Label(panel, text="SIN DATOS")
        self.light_label.grid(row=0, column=1, sticky="w")
        self.on_btn = ttk.Button(panel, text="Encender", command=lambda: self.light(True))
        self.on_btn.grid(row=0, column=2, padx=6)
        self.off_btn = ttk.Button(panel, text="Apagar luz", command=lambda: self.light(False))
        self.off_btn.grid(row=0, column=3, padx=12)

        ttk.Label(panel, text="Brillo (0–255)").grid(row=1, column=0, columnspan=4, pady=(8, 0))
        self.minus = ttk.Button(panel, text="−", width=4, command=lambda: self.step(-1))
        self.minus.grid(row=2, column=0, padx=12, pady=12)
        self.slider = ttk.Scale(panel, from_=0, to=255, variable=self.value,
                                command=self.slider_changed)
        self.slider.grid(row=2, column=1, columnspan=2, padx=8, sticky="ew")
        self.slider.bind("<ButtonRelease-1>", self.slider_released)
        self.plus = ttk.Button(panel, text="+", width=4, command=lambda: self.step(1))
        self.plus.grid(row=2, column=3, padx=12)
        self.value_label = ttk.Label(panel, text="0", font=("Sans", 18, "bold"))
        self.value_label.grid(row=3, column=0, columnspan=4, pady=(0, 8))

        profile_frame = ttk.LabelFrame(panel, text="Perfiles de brillo por filtro")
        profile_frame.grid(row=4, column=0, columnspan=4,
                           padx=12, pady=(4, 8), sticky="ew")
        profile_frame.columnconfigure(1, weight=1)
        ttk.Label(profile_frame, text="Filtro:").grid(
            row=0, column=0, padx=10, pady=9, sticky="w")
        self.filter_select = ttk.Combobox(
            profile_frame, textvariable=self.filter_var,
            values=FILTERS, state="readonly", width=15
        )
        self.filter_select.grid(row=0, column=1, padx=8, pady=9, sticky="ew")
        self.filter_select.bind("<<ComboboxSelected>>", self.on_filter_selected)
        self.apply_profile_btn = ttk.Button(
            profile_frame, text="Aplicar perfil",
            command=self.apply_selected_profile
        )
        self.apply_profile_btn.grid(row=0, column=2, padx=6, pady=9)
        self.save_profile_btn = ttk.Button(
            profile_frame, text="Guardar brillo actual",
            command=self.save_current_profile
        )
        self.save_profile_btn.grid(row=0, column=3, padx=(6, 10), pady=9)
        ttk.Label(profile_frame, textvariable=self.profile_info).grid(
            row=1, column=0, columnspan=4, padx=10, pady=(0, 9), sticky="w"
        )
        self.update_profile_info()

        bottom = ttk.Frame(panel)
        bottom.grid(row=5, column=0, columnspan=4, sticky="ew", padx=12, pady=10)
        self.refresh_btn = ttk.Button(bottom, text="Actualizar estado", command=self.refresh)
        self.refresh_btn.pack(side="left")
        self.finish_btn = ttk.Button(bottom, text="Finalizar sesión", command=self.finish)
        self.finish_btn.pack(side="right")

        log_frame = ttk.LabelFrame(self, text="Registro de actividad")
        log_frame.grid(row=4, column=0, padx=20, pady=5, sticky="nsew")
        self.log_text = tk.Text(log_frame, height=6, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.footer = ttk.Label(self, text=f"Log: {LOG_FILE}", anchor="w")
        self.footer.grid(row=5, column=0, padx=20, pady=(4, 12), sticky="ew")
        self._controls()

    def apply_theme(self):
        """Paleta oscura roja aplicada a todos los controles de la ventana."""
        night = self.night_mode.get()
        colors = (
            dict(bg="#090000", panel="#140000", input="#1c0000",
                 fg="#ff4545", accent="#b32020", active="#3d0808",
                 disabled="#823838", trough="#260404", led_on="#ff2424",
                 led_off="#390808", led_unknown="#8c2626")
            if night else
            dict(bg="#f0f0f0", panel="#f0f0f0", input="#ffffff",
                 fg="#202020", accent="#3478b5", active="#d9e8f7",
                 disabled="#777777", trough="#d0d0d0", led_on="#22b35d",
                 led_off="#777777", led_unknown="#d8a52a")
        )
        # 'clam' permite colorear también los botones, barras y casillas ttk.
        self._style.theme_use("clam")
        self.configure(bg=colors["bg"])
        self._style.configure("TFrame", background=colors["bg"])
        self._style.configure("TLabelframe", background=colors["panel"],
                              bordercolor=colors["accent"] if night else "#aaaaaa")
        self._style.configure("TLabelframe.Label", background=colors["panel"],
                              foreground=colors["fg"])
        self._style.configure("TLabel", background=colors["bg"], foreground=colors["fg"])
        self._style.configure("TLabelframe.TLabel", background=colors["panel"],
                              foreground=colors["fg"])
        self._style.configure("TEntry", fieldbackground=colors["input"],
                              foreground=colors["fg"],
                              background=colors["panel"],
                              bordercolor=colors["accent"],
                              insertcolor=colors["fg"])
        self._style.map("TEntry",
            fieldbackground=[("disabled", colors["panel"]),
                             ("readonly", colors["panel"])],
            foreground=[("disabled", colors["disabled"])])
        self._style.configure("TButton", background=colors["panel"],
                              foreground=colors["fg"], bordercolor=colors["accent"],
                              padding=5)
        self._style.map("TButton",
            background=[("disabled", colors["bg"]), ("active", colors["active"])],
            foreground=[("disabled", colors["disabled"]), ("active", colors["fg"])])
        self._style.configure("TCheckbutton", background=colors["bg"],
                              foreground=colors["fg"])
        self._style.map("TCheckbutton",
            background=[("active", colors["bg"])],
            foreground=[("active", colors["fg"])])
        self._style.configure("Horizontal.TScale", background=colors["panel"],
                              troughcolor=colors["trough"])
        self._style.map("Horizontal.TScale",
            background=[("active", colors["accent"])])
        self.led.configure(bg=colors["panel"])
        self.log_text.configure(bg=colors["input"], fg=colors["fg"],
                                insertbackground=colors["fg"],
                                selectbackground=colors["active"],
                                highlightbackground=colors["accent"],
                                highlightcolor=colors["accent"])
        # Mantener el estado LED conocido, sin modificar el estado físico.
        state = self.light_label.cget("text")
        led_color = (colors["led_on"] if state == "ENCENDIDO" else
                     colors["led_off"] if state == "APAGADO" else
                     colors["led_unknown"])
        self.led.itemconfig(self.dot, fill=led_color)

    def log(self, msg, level="info"):
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{timestamp}  {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        if level == "error":
            logger.error(msg)
        else:
            logger.info(msg)

    def initial_check(self):
        try:
            self.controller.prerequisites()
        except IndiError as exc:
            self.log(str(exc), "error")
            messagebox.showerror("Dependencias", str(exc))
            return
        if self.controller.server_alive():
            self.server_label.config(text="● Activo (externo)")
            self.log("Servidor INDI detectado")
        else:
            self.server_label.config(text="● Detenido")
        self._controls()

    def _controls(self):
        ready = self.connected and not self.busy
        for widget in (self.on_btn, self.off_btn, self.minus, self.plus,
                       self.slider, self.refresh_btn, self.finish_btn):
            widget.config(state="normal" if ready else "disabled")
        self.filter_select.config(state="readonly" if not self.busy else "disabled")
        self.save_profile_btn.config(
            state="normal" if self.connected and not self.busy else "disabled"
        )
        self.apply_profile_btn.config(
            state="normal" if self.connected and not self.busy
            and self.profiles.values.get(self.filter_var.get()) is not None
            else "disabled"
        )
        self.port_entry.config(
            state="normal" if not self.busy and not self.connected else "disabled"
        )
        self.connect_btn.config(
            state="normal" if not self.busy and not self.connected and self.controller.server_alive()
            else "disabled"
        )
        self.server_btn.config(
            state="normal" if not self.busy and not self.controller.server_alive()
            else "disabled"
        )

    def _submit(self, fn, callback, label, failure=None):
        if self.busy or self.closing:
            return
        self.busy = True
        self.log(label)
        self._controls()
        self.pending = (self.executor.submit(fn), callback, failure)
        self.after(100, self._poll)

    def _poll(self):
        if self.closing or self.pending is None:
            return
        future, callback, failure = self.pending
        if not future.done():
            self.after(100, self._poll)
            return
        self.pending = None
        self.busy = False
        try:
            result = future.result()
            callback(result)
        except Exception as exc:
            msg = str(exc)
            self.log("ERROR: " + msg, "error")
            if failure:
                failure()
            if not self.controller.server_alive():
                self.connected = False
                self.server_label.config(text="● Detenido")
                self.conn_label.config(text="● Desconectado")
                self.show_snapshot((None, None))
            messagebox.showerror("INDI / Flip Flat", msg)
        finally:
            self._controls()

    def start_server(self):
        def done(msg):
            self.server_label.config(text="● Activo")
            self.log(msg)
        self._submit(self.controller.start, done, "Iniciando servidor INDI...")

    def connect(self):
        selected_port = self.serial_port_var.get().strip()
        if not selected_port:
            messagebox.showerror("Puerto serie", "Indica un puerto serie válido.")
            return
        if not selected_port.startswith("/dev/"):
            messagebox.showerror(
                "Puerto serie",
                "El puerto debe ser una ruta de dispositivo Linux, por ejemplo /dev/ttyUSB0."
            )
            return
        self.controller.serial_port = selected_port
        self.log(f"Puerto serie seleccionado: {selected_port}")

        profile = self.filter_var.get()
        saved = self.profiles.values.get(profile)

        def task():
            snap = self.controller.connect()
            if saved is not None:
                snap = self.controller.set_brightness(saved)
            return snap

        def done(snap):
            self.connected = True
            self.conn_label.config(text="● Conectado")
            self.show_snapshot(snap)
            self.log("Panel conectado")
            if saved is not None:
                self.log(f"Perfil {profile} recuperado: {saved}/255")
        self._submit(task, done, "Conectando al panel...")

    def update_profile_info(self):
        name = self.filter_var.get()
        value = self.profiles.values.get(name)
        self.profile_info.set(
            f"{name}: brillo guardado {value}/255"
            if value is not None else f"{name}: sin brillo guardado"
        )

    def on_filter_selected(self, _event=None):
        name = self.filter_var.get()
        if name not in FILTERS:
            return
        self.profiles.selected = name
        try:
            self.profiles.save()
        except OSError as exc:
            self.log(f"No se pudo guardar el filtro seleccionado: {exc}", "error")
        self.update_profile_info()
        self._controls()
        value = self.profiles.values[name]
        if value is None:
            self.log(f"Filtro {name}: sin valor guardado")
            return
        self.value.set(value)
        self.value_label.config(text=str(value))
        if self.connected:
            self.apply_brightness(profile=name)
        else:
            self.log(f"Filtro {name}: brillo {value} preparado para la conexión")

    def apply_selected_profile(self):
        """Vuelve a aplicar explícitamente el brillo almacenado del filtro."""
        if not self.connected or self.busy:
            return
        name = self.filter_var.get()
        saved = self.profiles.values.get(name)
        if saved is None:
            messagebox.showinfo(
                "Perfil sin guardar",
                f"El filtro {name} todavía no tiene un brillo guardado."
            )
            return
        self.value.set(saved)
        self.value_label.config(text=str(saved))
        self.apply_brightness(profile=name)

    def save_current_profile(self):
        if not self.connected or self.busy:
            return
        name = self.filter_var.get()
        self._submit(
            self.controller.refresh,
            lambda snap: self._save_profile_after_refresh(name, snap),
            f"Leyendo brillo real para guardar perfil {name}..."
        )

    def _save_profile_after_refresh(self, name, snap):
        self.show_snapshot(snap)
        brightness, _ = snap
        if brightness is None:
            messagebox.showerror("Perfiles", "INDI no devolvió el brillo real.")
            return
        try:
            self.profiles.set_value(name, brightness)
            self.log(f"Perfil {name} guardado: {brightness}/255")
            self.update_profile_info()
            self._controls()
        except OSError as exc:
            messagebox.showerror("Perfiles", f"No se pudo guardar el perfil: {exc}")

    def _brightness_applied(self, name, snap):
        self.show_snapshot(snap)
        brightness, _ = snap
        if brightness is not None:
            try:
                self.profiles.set_value(name, brightness)
                self.update_profile_info()
                self.log(f"Perfil {name} actualizado automáticamente: {brightness}/255")
            except OSError as exc:
                self.log(f"Brillo aplicado, pero no guardado: {exc}", "error")

    def show_snapshot(self, snap):
        brightness, lit = snap
        if brightness is not None:
            self._updating = True
            self.value.set(brightness)
            self.value_label.config(text=str(brightness))
            self._updating = False
        if lit is True:
            self.led.itemconfig(self.dot, fill="#ff2424" if self.night_mode.get() else "#22b35d")
            self.light_label.config(text="ENCENDIDO")
        elif lit is False:
            self.led.itemconfig(self.dot, fill="#390808" if self.night_mode.get() else "#777777")
            self.light_label.config(text="APAGADO")
        else:
            self.led.itemconfig(self.dot, fill="#8c2626" if self.night_mode.get() else "#d8a52a")
            self.light_label.config(text="SIN CONFIRMACIÓN")

    def light(self, enabled):
        self._submit(
            lambda: self.controller.set_light(enabled),
            self.show_snapshot,
            "Encendiendo iluminación..." if enabled else "Apagando iluminación..."
        )

    def refresh(self):
        self._submit(self.controller.refresh, self.show_snapshot, "Actualizando estado...")

    def finish(self):
        def done(msg):
            self.connected = False
            self.conn_label.config(text="● Desconectado")
            self.show_snapshot((0, False))
            self.server_label.config(
                text="● Activo (externo)" if self.controller.server_alive() else "● Detenido"
            )
            self.log("Sesión finalizada: " + msg)
        self._submit(self.controller.finish, done, "Apagando, desconectando y cerrando sesión...")

    def slider_changed(self, raw):
        if not self._updating:
            self.value_label.config(text=str(round(float(raw))))

    def slider_released(self, _event):
        if self.connected and not self.busy:
            self.apply_brightness()

    def step(self, delta):
        if not self.connected or self.busy:
            return
        val = max(0, min(255, round(self.slider.get()) + delta))
        self.value.set(val)
        self.value_label.config(text=str(val))
        self.apply_brightness()

    def apply_brightness(self, profile=None):
        if not self.connected or self.busy:
            return
        val = max(0, min(255, round(self.slider.get())))
        name = profile or self.filter_var.get()
        self._submit(
            lambda: self.controller.set_brightness(val),
            lambda snap: self._brightness_applied(name, snap),
            f"Ajustando brillo a {val} para {name}..."
        )

    def on_close(self):
        if self.busy:
            messagebox.showinfo(
                "Operación en curso",
                "Espera a que termine la operación INDI antes de cerrar."
            )
            return
        if self.connected:
            if not messagebox.askyesno(
                "Cerrar",
                "El panel está conectado. ¿Apagar la iluminación y finalizar la sesión?"
            ):
                return
            self._submit(self.controller.finish, self._close_after_finish,
                         "Finalizando antes de cerrar...")
            return
        self._close_after_finish(None)

    def _close_after_finish(self, _):
        self.closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.controller.stop_owned()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
