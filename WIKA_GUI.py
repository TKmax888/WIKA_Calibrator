"""WIKA CTM9100 – Grafische Benutzeroberfläche"""
import tkinter as tk
from tkinter import ttk
from pymodbus.client import ModbusSerialClient
import serial.tools.list_ports
import csv
import os
import threading
import time
from datetime import datetime

BAUDRATE         = 9600
SLAVE_ID         = 1
POLL_INTERVAL_MS = 1000

ADDR_PV    = 0x0200
ADDR_DP    = 0x0201
ADDR_SP    = 0x0208
ADDR_SPAT  = 0x2801
ADDR_MODUS = 0x0380

SP_NAMEN = {1: "SP1", 2: "SP2", 3: "SP3", 4: "SP4"}

MODUS_LESEN = {3: "LI", 4: "db", 5: "Ir", 6: "SU"}
MODUS_NAMEN = {"LI": 3, "db": 4, "Ir": 5, "SU": 6}
MODUS_BESCHREIBUNG = {
    "LI": "LI – Mikrokalibrierbad",
    "db": "db – Blockkalibrator",
    "Ir": "Ir – Infrarot",
    "SU": "SU – Oberfläche",
}

def lese_messwerte(client):
    try:
        r_pv   = client.read_holding_registers(ADDR_PV,    count=1, device_id=SLAVE_ID)
        r_dp   = client.read_holding_registers(ADDR_DP,    count=1, device_id=SLAVE_ID)
        r_sp   = client.read_holding_registers(ADDR_SP,    count=1, device_id=SLAVE_ID)
        r_spat = client.read_holding_registers(ADDR_SPAT,  count=1, device_id=SLAVE_ID)
        r_mod  = client.read_holding_registers(ADDR_MODUS, count=1, device_id=SLAVE_ID)
    except Exception:
        return None, None, None, None, None

    if r_pv.isError() or r_dp.isError():
        return None, None, None, None, None

    raw_pv = r_pv.registers[0]
    if raw_pv > 32767:
        raw_pv -= 65536
    if raw_pv in (-10000, 32000, 32001, 32003):
        return None, None, None, None, None

    dp = r_dp.registers[0]
    if not (0 <= dp <= 3):
        return None, None, None, None, None

    pv = raw_pv / (10 ** dp)

    sp = None
    if not r_sp.isError() and r_sp.registers:
        raw_sp = r_sp.registers[0]
        if raw_sp > 32767:
            raw_sp -= 65536
        sp = raw_sp / (10 ** dp)

    sp_index = None
    if not r_spat.isError() and r_spat.registers:
        sp_index = r_spat.registers[0]

    modus_key = None
    if not r_mod.isError() and r_mod.registers:
        modus_key = MODUS_LESEN.get(r_mod.registers[0])

    return pv, sp, dp, sp_index, modus_key


class WikaApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("WIKA CTM9100")
        self.root.resizable(False, False)
        self.client         = None
        self._poll_job      = None
        self._csv_file      = None
        self._csv_writer    = None
        self._csv_path      = None
        self._modus_aktuell = None
        self._stab_start    = None
        self._letzter_sp    = None
        self._stop_job      = None

        self._build_ui()
        self._refresh_ports()

    def _build_ui(self):
        PAD = 14
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=1)

        conn = ttk.LabelFrame(self.root, text="Verbindung", padding=PAD)
        conn.grid(row=0, column=0, padx=(PAD, 6), pady=(PAD, 6), sticky="nsew")

        ttk.Label(conn, text="COM-Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            conn, textvariable=self.port_var, width=10, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, padx=(6, 2))
        ttk.Button(conn, text="↻", width=3, command=self._refresh_ports).grid(
            row=0, column=2, padx=(0, 14)
        )
        self.connect_btn = ttk.Button(
            conn, text="Verbinden", width=14, command=self._toggle_connection
        )
        self.connect_btn.grid(row=0, column=3)
        self.status_lbl = ttk.Label(conn, text="● Getrennt", foreground="#CC0000")
        self.status_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))

        mode = ttk.LabelFrame(self.root, text="Betriebsart", padding=PAD)
        mode.grid(row=0, column=1, padx=(0, PAD), pady=(PAD, 6), sticky="nsew")

        ttk.Label(mode, text="Aktuell:", font=("Segoe UI", 10)).grid(
            row=0, column=0, sticky="w"
        )
        self.modus_var = tk.StringVar(value="–")
        ttk.Label(
            mode, textvariable=self.modus_var,
            font=("Segoe UI", 10, "bold"), foreground="#0063B1", width=24
        ).grid(row=0, column=1, columnspan=2, sticky="w", padx=(8, 0))

        ttk.Label(mode, text="Ändern:", font=("Segoe UI", 10)).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.modus_combo_var = tk.StringVar()
        self.modus_combo = ttk.Combobox(
            mode,
            textvariable=self.modus_combo_var,
            values=list(MODUS_BESCHREIBUNG.values()),
            state="disabled",
            width=24,
        )
        self.modus_combo.grid(row=1, column=1, padx=(8, 8), pady=(8, 0))
        self.modus_set_btn = ttk.Button(
            mode, text="Übernehmen", width=14,
            command=self._modus_setzen, state="disabled"
        )
        self.modus_set_btn.grid(row=1, column=2, pady=(8, 0))
        self.modus_info_lbl = ttk.Label(
            mode, text="", foreground="#666666", font=("Segoe UI", 9)
        )
        self.modus_info_lbl.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        disp = ttk.LabelFrame(self.root, text="Messwerte", padding=PAD)
        disp.grid(row=1, column=0, columnspan=2, padx=PAD, pady=(0, 6), sticky="ew")
        disp.columnconfigure((0, 1, 2), weight=1)

        FONT_LBL  = ("Segoe UI", 11)
        FONT_BIG  = ("Segoe UI", 40, "bold")
        FONT_UNIT = ("Segoe UI", 20)

        ttk.Label(disp, text="Aktiver Sollpunkt", font=FONT_LBL).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.sp_name_var = tk.StringVar(value="–")
        ttk.Label(
            disp, textvariable=self.sp_name_var,
            font=FONT_BIG, foreground="#0063B1", width=5, anchor="w"
        ).grid(row=1, column=0, sticky="w", padx=(0, 30))

        ttk.Label(disp, text="Solltemperatur", font=FONT_LBL).grid(
            row=0, column=1, sticky="w", pady=(0, 4)
        )
        self.sp_temp_var = tk.StringVar(value="–")
        sp_row = tk.Frame(disp)
        sp_row.grid(row=1, column=1, sticky="w", padx=(0, 30))
        ttk.Label(sp_row, textvariable=self.sp_temp_var,
                  font=FONT_BIG, anchor="w").pack(side="left")
        ttk.Label(sp_row, text=" °C", font=FONT_UNIT).pack(
            side="left", anchor="s", pady=(0, 6)
        )

        ttk.Label(disp, text="Isttemperatur", font=FONT_LBL).grid(
            row=0, column=2, sticky="w", pady=(0, 4)
        )
        self.pv_var = tk.StringVar(value="–")
        pv_row = tk.Frame(disp)
        pv_row.grid(row=1, column=2, sticky="w")
        self.pv_lbl = ttk.Label(
            pv_row, textvariable=self.pv_var, font=FONT_BIG, anchor="w"
        )
        self.pv_lbl.pack(side="left")
        ttk.Label(pv_row, text=" °C", font=FONT_UNIT).pack(
            side="left", anchor="s", pady=(0, 6)
        )

        ttk.Separator(disp, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(16, 8)
        )
        schwelle_frame = tk.Frame(disp)
        schwelle_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(
            schwelle_frame,
            text="Stabilitätsdauer:",
            font=("Segoe UI", 10)
        ).pack(side="left")
        self.schwellwert_var = tk.IntVar(value=20)
        self.schwellwert_spin = ttk.Spinbox(
            schwelle_frame, from_=5, to=300, increment=5,
            textvariable=self.schwellwert_var, width=6
        )
        self.schwellwert_spin.pack(side="left", padx=(10, 4))
        ttk.Label(schwelle_frame, text="s", font=("Segoe UI", 10)).pack(side="left")
        ttk.Label(schwelle_frame, text="     Toleranz:", font=("Segoe UI", 10)).pack(side="left")
        self.toleranz_var = tk.DoubleVar(value=0.3)
        self.toleranz_spin = ttk.Spinbox(
            schwelle_frame, from_=0.1, to=5.0, increment=0.1,
            textvariable=self.toleranz_var, width=6, format="%.1f"
        )
        self.toleranz_spin.pack(side="left", padx=(10, 4))
        ttk.Label(schwelle_frame, text="°C", font=("Segoe UI", 10)).pack(side="left")

        self.stab_lbl = ttk.Label(
            disp, text="", foreground="#666666", font=("Segoe UI", 9, "italic")
        )
        self.stab_lbl.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        sp_cfg = ttk.LabelFrame(self.root, text="Sollpunkt-Auswahl", padding=PAD)
        sp_cfg.grid(row=2, column=0, columnspan=2, padx=PAD, pady=(0, 6), sticky="ew")

        self._sp_enabled   = [tk.BooleanVar(value=(i < 3)) for i in range(4)]
        self._sp_wert_vars = [tk.StringVar(value="") for _ in range(4)]
        self._sp_cb_list   = []
        for i in range(4):
            col_base = i * 3
            cb = ttk.Checkbutton(
                sp_cfg, text=f"SP{i + 1}",
                variable=self._sp_enabled[i],
                width=5,
            )
            cb.grid(row=0, column=col_base, sticky="w", padx=(0 if i == 0 else 18, 2))
            ttk.Entry(sp_cfg, textvariable=self._sp_wert_vars[i], width=8, state="readonly").grid(
                row=0, column=col_base + 1, padx=(2, 2)
            )
            ttk.Label(sp_cfg, text="°C").grid(row=0, column=col_base + 2, sticky="w")
            self._sp_cb_list.append(cb)

        log_frame = ttk.LabelFrame(self.root, text="Aufzeichnung", padding=PAD)
        log_frame.grid(row=3, column=0, columnspan=2, padx=PAD, pady=(0, 6), sticky="ew")

        self.log_btn = ttk.Button(
            log_frame, text="⏺  Aufzeichnung starten", width=26,
            command=self._toggle_logging, state="disabled"
        )
        self.log_btn.grid(row=0, column=0, sticky="w")
        self.log_status_lbl = ttk.Label(
            log_frame, text="", foreground="#666666", font=("Segoe UI", 9)
        )
        self.log_status_lbl.grid(row=0, column=1, padx=(16, 0), sticky="w")

        ttk.Separator(self.root, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=PAD
        )
        self.info_lbl = ttk.Label(
            self.root, text="", foreground="#666666",
            font=("Segoe UI", 9)
        )
        self.info_lbl.grid(row=5, column=0, columnspan=2, sticky="w", padx=PAD, pady=(4, PAD))

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            preferred = "COM5"
            self.port_combo.set(preferred if preferred in ports else ports[0])

    def _toggle_connection(self):
        if self.client and self.client.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            return
        self.status_lbl.config(text="● Verbinde …", foreground="#CC6600")
        self.root.update_idletasks()
        try:
            c = ModbusSerialClient(
                port=port, baudrate=BAUDRATE,
                bytesize=8, parity="N", stopbits=1,
                timeout=1, handle_local_echo=True,
            )
            if c.connect():
                self.client = c
                self.status_lbl.config(
                    text=f"● Verbunden  ({port}, {BAUDRATE} Baud)",
                    foreground="#007A00"
                )
                self.connect_btn.config(text="Trennen")
                self.port_combo.config(state="disabled")
                self.log_btn.config(state="normal")
                self.modus_combo.config(state="readonly")
                self.modus_set_btn.config(state="normal")
                self._poll()
                self.root.after(300, lambda: threading.Thread(
                    target=self._lese_alle_sp_werte, daemon=True
                ).start())
            else:
                self.status_lbl.config(
                    text="● Verbindung fehlgeschlagen", foreground="#CC0000"
                )
        except Exception as e:
            self.status_lbl.config(text=f"● Fehler: {e}", foreground="#CC0000")

    def _disconnect(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self._stop_logging()
        self.status_lbl.config(text="● Getrennt", foreground="#CC0000")
        self.connect_btn.config(text="Verbinden", state="normal")
        self.port_combo.config(state="readonly")
        self.log_btn.config(state="disabled")
        self.modus_combo.config(state="disabled")
        self.modus_set_btn.config(state="disabled")
        self.modus_var.set("–")
        self.modus_combo_var.set("")
        self.modus_info_lbl.config(text="")
        self.sp_name_var.set("–")
        self.sp_temp_var.set("–")
        self.pv_var.set("–")
        self.pv_lbl.config(foreground="black")
        self.info_lbl.config(text="")

    def _lese_alle_sp_werte(self):
        """Hintergrund-Thread: liest SP1-4 Sollwerte, stellt Original-SP wieder her."""
        if not (self.client and self.client.connected):
            return
        self.root.after(0, self._pause_poll)
        time.sleep(0.1)
        try:
            r_dp = self.client.read_holding_registers(ADDR_DP, count=1, device_id=SLAVE_ID)
            r_orig = self.client.read_holding_registers(ADDR_SPAT, count=1, device_id=SLAVE_ID)
            if r_dp.isError() or r_orig.isError():
                return
            dp = r_dp.registers[0]
            if not (0 <= dp <= 3):
                return
            original_spat = r_orig.registers[0]
            results = {}
            for i in range(1, 5):
                self.client.write_register(ADDR_SPAT, i, device_id=SLAVE_ID)
                time.sleep(0.4)
                r_sp = self.client.read_holding_registers(ADDR_SP, count=1, device_id=SLAVE_ID)
                if not r_sp.isError() and r_sp.registers:
                    raw = r_sp.registers[0]
                    if raw > 32767:
                        raw -= 65536
                    results[i] = raw / (10 ** dp)
            self.client.write_register(ADDR_SPAT, original_spat, device_id=SLAVE_ID)
            time.sleep(0.2)
            def _update():
                for i, val in results.items():
                    if not self._sp_wert_vars[i - 1].get():
                        self._sp_wert_vars[i - 1].set(f"{val:.1f}")
                self._poll()
            self.root.after(0, _update)
        except Exception:
            self.root.after(0, self._poll)

    def _pause_poll(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _poll(self):
        if not (self.client and self.client.connected):
            self._disconnect()
            return

        pv, sp, _, sp_index, modus_key = lese_messwerte(self.client)

        if pv is not None:
            self.pv_var.set(f"{pv:.1f}")
            self.sp_temp_var.set(f"{sp:.1f}" if sp is not None else "–")
            sp_name = SP_NAMEN.get(sp_index, f"SP{sp_index}") if sp_index else "–"
            self.sp_name_var.set(sp_name)

            if modus_key and modus_key != self._modus_aktuell:
                self._modus_aktuell = modus_key
                beschr = MODUS_BESCHREIBUNG.get(modus_key, modus_key)
                self.modus_var.set(beschr)
                self.modus_combo_var.set(beschr)

            if sp is not None:
                diff     = abs(pv - sp)
                toleranz = self.toleranz_var.get()
                if diff <= toleranz:
                    color = "#007A00"
                elif diff <= 2.0:
                    color = "#CC6600"
                else:
                    color = "#CC0000"
                self.pv_lbl.config(foreground=color)

                if sp_index and 1 <= sp_index <= 4:
                    if not self._sp_wert_vars[sp_index - 1].get():
                        self._sp_wert_vars[sp_index - 1].set(f"{sp:.1f}")

                if self._csv_file is not None:
                    if sp_index != self._letzter_sp:
                        self._stab_start = None
                        self._letzter_sp = sp_index

                    aktive_sps = sorted(
                        [i + 1 for i, v in enumerate(self._sp_enabled) if v.get()]
                    )

                    if not aktive_sps:
                        self.stab_lbl.config(
                            text="Kein SP ausgewählt – nur Aufzeichnung läuft",
                            foreground="#666666"
                        )
                    elif sp_index not in aktive_sps:
                        self.stab_lbl.config(
                            text=f"SP{sp_index} nicht ausgewählt – warte auf nächsten aktiven SP",
                            foreground="#666666"
                        )
                    elif diff <= toleranz:
                        if self._stab_start is None:
                            self._stab_start = datetime.now()
                        elapsed  = (datetime.now() - self._stab_start).total_seconds()
                        schwelle = self.schwellwert_var.get()
                        if elapsed >= schwelle:
                            idx = aktive_sps.index(sp_index)
                            if idx + 1 < len(aktive_sps):
                                naechster = aktive_sps[idx + 1]
                                try:
                                    self.client.write_register(
                                        ADDR_SPAT, naechster, device_id=SLAVE_ID
                                    )
                                    self._stab_start = None
                                    self.stab_lbl.config(
                                        text=f"✓ SP{sp_index} stabil → SP{naechster} aktiviert",
                                        foreground="#007A00"
                                    )
                                except Exception:
                                    pass
                            else:
                                self.stab_lbl.config(
                                    text="✓ Alle ausgewählten Sollpunkte abgeschlossen – Aufzeichnung wird beendet",
                                    foreground="#007A00"
                                )
                                if self._stop_job is None:
                                    self._stop_job = self.root.after(1500, self._stop_logging)
                        else:
                            verbleibend = int(schwelle - elapsed)
                            self.stab_lbl.config(
                                text=f"Stabil seit {int(elapsed)} s – Umschaltung in {verbleibend} s",
                                foreground="#007A00"
                            )
                    else:
                        self._stab_start = None
                        self.stab_lbl.config(
                            text=f"Warte auf Stabilität (±{toleranz:.1f} °C) – Abweichung {diff:.1f} °C",
                            foreground="#CC6600"
                        )
                else:
                    self.stab_lbl.config(text="")

            self._log_row(sp_name, sp, pv)
            self.info_lbl.config(
                text=f"Letzte Aktualisierung: {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            self.pv_var.set("Fehler")
            self.pv_lbl.config(foreground="#CC0000")
            self.info_lbl.config(text="Lesefehler – Verbindung prüfen")

        self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll)

    def _modus_setzen(self):
        auswahl  = self.modus_combo_var.get()
        kuerzel  = auswahl.split(" – ")[0].strip() if " – " in auswahl else auswahl.strip()
        reg_wert = MODUS_NAMEN.get(kuerzel)
        if reg_wert is None:
            self.modus_info_lbl.config(
                text="Unbekannte Betriebsart.", foreground="#CC0000"
            )
            return
        if not (self.client and self.client.connected):
            return
        try:
            r = self.client.write_register(ADDR_MODUS, reg_wert, device_id=SLAVE_ID)
            if r.isError():
                self.modus_info_lbl.config(
                    text="Schreibfehler – Gerät hat Änderung abgelehnt.",
                    foreground="#CC0000"
                )
            else:
                self._modus_aktuell = None
                self.modus_info_lbl.config(
                    text=f"Betriebsart auf '{kuerzel}' gesetzt.",
                    foreground="#007A00"
                )
        except Exception as e:
            self.modus_info_lbl.config(text=f"Fehler: {e}", foreground="#CC0000")

    def _toggle_logging(self):
        if self._csv_file is None:
            self._start_logging()
        else:
            self._stop_logging()

    def _start_logging(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._csv_path = os.path.join(
            script_dir,
            datetime.now().strftime("WIKA_%Y%m%d_%H%M%S.csv")
        )
        self._csv_file = open(self._csv_path, 'w', newline='', encoding='utf-8')
        self._csv_writer = csv.writer(self._csv_file, delimiter=';')
        self._csv_writer.writerow(['Timestamp', 'Sollpunkt', 'Sollwert_C', 'Messwert_C'])
        self._csv_file.flush()
        self._stab_start = None
        self._letzter_sp = None
        self.stab_lbl.config(text=f"Warte auf Stabilität (±{self.toleranz_var.get():.1f} °C)", foreground="#666666")
        # Zum ersten ausgewählten SP springen
        aktive_sps = sorted([i + 1 for i, v in enumerate(self._sp_enabled) if v.get()])
        if aktive_sps and self.client and self.client.connected:
            try:
                self.client.write_register(ADDR_SPAT, aktive_sps[0], device_id=SLAVE_ID)
            except Exception:
                pass
        self.modus_combo.config(state="disabled")
        self.modus_set_btn.config(state="disabled")
        self.schwellwert_spin.config(state="disabled")
        self.toleranz_spin.config(state="disabled")
        for cb in self._sp_cb_list:
            cb.config(state="disabled")
        self.connect_btn.config(state="disabled")
        self.log_btn.config(text="⏹  Aufzeichnung stoppen")
        self.log_status_lbl.config(
            text=f"● Schreibt: {os.path.basename(self._csv_path)}",
            foreground="#007A00"
        )

    def _stop_logging(self):
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self.log_status_lbl.config(
                text=f"Gespeichert: {os.path.basename(self._csv_path)}",
                foreground="#666666"
            )
        self._csv_file   = None
        self._csv_writer = None
        self._stab_start = None
        self._letzter_sp = None
        self._stop_job   = None
        self.stab_lbl.config(text="", foreground="#666666")
        if self.client and self.client.connected:
            self.modus_combo.config(state="readonly")
            self.modus_set_btn.config(state="normal")
            self.connect_btn.config(state="normal")
        self.schwellwert_spin.config(state="normal")
        self.toleranz_spin.config(state="normal")
        for cb in self._sp_cb_list:
            cb.config(state="normal")
        self.log_btn.config(text="⏺  Aufzeichnung starten")

    def _log_row(self, sp_name, sp, pv):
        if self._csv_writer is None:
            return
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sp_str = f"{sp:.2f}" if sp is not None else ""
        self._csv_writer.writerow([ts, sp_name, sp_str, f"{pv:.2f}"])
        self._csv_file.flush()


if __name__ == "__main__":
    root = tk.Tk()
    app = WikaApp(root)
    root.mainloop()
