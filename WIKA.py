from pymodbus.client import ModbusSerialClient
import argparse
import logging
import time
import csv
from datetime import datetime

# Optional: Konfigurieren Sie das Logging für pymodbus, um Debug-Informationen zu sehen
# logging.basicConfig()
# log = logging.getLogger()
# log.setLevel(logging.DEBUG)

def connect_k32k_modbus_rtu(port, baudrate=1200, timeout=1, slave_id=1):
    """
    Stellt eine Verbindung zu einem K32K-Controller über Modbus RTU her.

    Basierend auf dem Dokument "K32K Communication protocol user's guide":
    - Physische Verbindung: RS485
    - Baudraten: 1200 bis 38400 (Standard: 1200, der niedrigste Wert laut Dokument)
    - Datenformat: 8 Bits, keine Parität, 1 Stoppbit (8N1)
    - Protokoll: Subset von MODBUS RTU (JBUS)

    Args:
        port (str): Der Name des seriellen Ports (z.B. 'COM1' unter Windows,
                    '/dev/ttyUSB0' unter Linux).
        baudrate (int): Die Baudrate der Kommunikation. Laut Dokument: 1200 bis 38400.
                        Standardwert ist 1200 (der niedrigste mögliche Wert).
        timeout (int): Timeout in Sekunden für die Antwort des Slaves (Standard: 1 Sekunde).
                       Wichtig für die Robustheit der Kommunikation.
        slave_id (int): Die Modbus-Slave-ID des K32K-Controllers (Standard: 1).
                        Diese ID wird bei jeder Anfrage an den Client übergeben.

    Returns:
        ModbusSerialClient or None: Ein konfiguriertes und verbundenes ModbusSerialClient-Objekt
                                    bei Erfolg, andernfalls None.
    """
    print(f"Versuche Verbindung zu K32K Controller auf Port '{port}' mit Baudrate {baudrate}...")

    try:
        # Erstelle einen ModbusSerialClient.
        # Der Modus ist 'rtu', da das Dokument dies als Subset von MODBUS RTU (JBUS) beschreibt.
        # Die Parameter bytesize, parity, stopbits sind laut Dokument 8N1.
        client = ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity='N',  # No Parity
            stopbits=1,
            timeout=timeout,
            handle_local_echo=True,  # TX-Echo des RS485-Adapters ignorieren
        )

        # Versuche, die Verbindung zum seriellen Port herzustellen
        if client.connect():
            print(f"Verbindung zu K32K Controller auf Port '{port}' erfolgreich hergestellt.")
            # Die Slave ID wird nicht direkt im Client-Objekt gespeichert,
            # sondern bei jeder Anfrage als Parameter übergeben.
            # Wir speichern sie hier als benutzerdefiniertes Attribut zur Bequemlichkeit.
            client.slave_id = slave_id
            return client
        else:
            print(f"Fehler: Verbindung zu K32K Controller auf Port '{port}' konnte nicht hergestellt werden.")
            return None

    except Exception as e:
        print(f"Ein unerwarteter Fehler ist beim Verbindungsaufbau aufgetreten: {e}")
        return None

# --- Beispiel zur Verwendung der Funktion ---
if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='WIKA CTM9100 Kalibrierungsmessung')
    parser.add_argument('--port', default='COM5', help='Serieller Port (Standard: COM5)')
    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # Konfiguration
    # -------------------------------------------------------------------------
    SERIAL_PORT = args.port
    BAUDRATE    = 9600      # Werkseinstellung WIKA CTM9100 (Kapitel 4.6)
    SLAVE_ID    = 1

    # Sollpunkte: (Name, SP-Index 1..4)
    # Die Temperaturwerte werden vorab am Gerät konfiguriert.
    # SP-Index wird in Register SPAt (0x2801) geschrieben.
    SOLLPUNKTE = [
        ('SP1', 1),
        ('SP2', 2),
        ('SP3', 3),
    ]

    # Adresse zum Umschalten des aktiven Sollpunkts (SPAt, laut Dok. Adresse 0x2801)
    SP_SCHREIB_ADRESSE = 0x2801

    # Stabilitätskriterien
    STABILITAET_TOLERANZ = 0.3   # ±°C um den Sollwert
    STABILITAET_DAUER    = 20    # Sekunden innerhalb der Toleranz

    # Registeradressen (Variables zone, Dokument 4.2.1)
    ADDR_PV  = 0x0200   # Messwert (signed integer)
    ADDR_DP  = 0x0201   # Anzahl Dezimalstellen
    ADDR_SP  = 0x0208   # Aktiver Sollwert (read-only)
    # -------------------------------------------------------------------------

    def lese_messwerte(client):
        """Liest PV, Dezimalstellen und aktiven Sollwert. Gibt (pv, sp, dp) zurück."""
        r_pv = client.read_holding_registers(ADDR_PV, count=1, device_id=SLAVE_ID)
        r_dp = client.read_holding_registers(ADDR_DP, count=1, device_id=SLAVE_ID)
        r_sp = client.read_holding_registers(ADDR_SP, count=1, device_id=SLAVE_ID)

        if r_pv.isError() or r_dp.isError():
            return None, None, None

        raw_pv = r_pv.registers[0]
        if raw_pv > 32767:
            raw_pv -= 65536
        if raw_pv in (-10000, 32000, 32001, 32003):
            return None, None, None

        dp = r_dp.registers[0]
        if not (0 <= dp <= 3):
            return None, None, None

        pv = raw_pv / (10 ** dp)

        sp = None
        if not r_sp.isError() and r_sp.registers:
            raw_sp = r_sp.registers[0]
            if raw_sp > 32767:
                raw_sp -= 65536
            sp = raw_sp / (10 ** dp)

        return pv, sp, dp

    def schreibe_sollpunkt(client, sp_index):
        """Aktiviert den Sollpunkt sp_index (1..4) über Modbus FC6 in Register SPAt (0x2801)."""
        r = client.write_register(SP_SCHREIB_ADRESSE, sp_index, device_id=SLAVE_ID)
        return not r.isError()

    # Verbindung herstellen
    client = connect_k32k_modbus_rtu(SERIAL_PORT, BAUDRATE, slave_id=SLAVE_ID)
    if not client:
        print("Verbindung fehlgeschlagen.")
        exit(1)

    print(f"Verbunden: {SERIAL_PORT}, {BAUDRATE} Baud, Slave {SLAVE_ID}")

    # Dezimalstellen einmalig lesen
    _, _, dp_init = lese_messwerte(client)
    dp_init = dp_init if dp_init is not None else 1

    # CSV-Datei anlegen
    csv_filename = datetime.now().strftime("WIKA_%Y%m%d_%H%M%S.csv")
    print(f"Messwerte werden gespeichert in: {csv_filename}")

    try:
        with open(csv_filename, 'w', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file, delimiter=';')
            writer.writerow(['Timestamp', 'Sollpunkt', 'Sollwert_C', 'Messwert_C'])

            for sp_name, sp_index in SOLLPUNKTE:
                print(f"\n{'='*55}")
                print(f"  {sp_name} (Index {sp_index}) aktivieren ...")
                print(f"{'='*55}")

                # Aktiven Sollpunkt umschalten
                if schreibe_sollpunkt(client, sp_index):
                    print(f"  {sp_name} erfolgreich aktiviert (SPAt = {sp_index}).")
                else:
                    print(f"  WARNUNG: Umschalten auf {sp_name} fehlgeschlagen!")

                # Kurz warten, damit das Gerät den neuen Sollwert übernimmt
                time.sleep(2)

                # Zieltemperatur aus dem aktiven SP auslesen
                _, sp_ziel, _ = lese_messwerte(client)
                if sp_ziel is None:
                    print(f"  WARNUNG: Zieltemperatur konnte nicht gelesen werden.")
                    sp_ziel = 0.0
                print(f"  Zieltemperatur: {sp_ziel}°C")

                stabil_seit = None

                while True:
                    pv, sp, dp = lese_messwerte(client)
                    if pv is None:
                        time.sleep(1)
                        continue

                    # Aktuelle Zieltemperatur immer frisch aus SP lesen
                    if sp is not None:
                        sp_ziel = sp

                    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    abweichung = abs(pv - sp_ziel)
                    stabil = abweichung <= STABILITAET_TOLERANZ

                    if stabil:
                        if stabil_seit is None:
                            stabil_seit = time.time()
                        verbleibend = max(0, STABILITAET_DAUER - int(time.time() - stabil_seit))
                        status = f"stabil ({verbleibend}s verbleibend)"
                    else:
                        stabil_seit = None
                        richtung = "↑" if pv < sp_ziel else "↓"
                        status = f"{richtung} Δ{abweichung:.1f}°C"

                    print(f"[{ts}] {sp_name}: {pv:.1f}°C | Soll: {sp_ziel}°C | {status}")
                    writer.writerow([ts, sp_name, sp_ziel, pv])
                    csv_file.flush()

                    if stabil and (time.time() - stabil_seit) >= STABILITAET_DAUER:
                        print(f"\n  ✓ {sp_name} stabil bei {pv:.1f}°C – weiter mit nächstem Sollpunkt.")
                        break

                    time.sleep(1)

        print(f"\n{'='*55}")
        print(f"  Alle Sollpunkte abgeschlossen.")
        print(f"  Messdatei: {csv_filename}")
        print(f"{'='*55}")

    except KeyboardInterrupt:
        print("\nMessung abgebrochen (Strg+C).")
    except Exception as e:
        print(f"Fehler: {e}")
    finally:
        if client.connected:
            client.close()
            print("Verbindung getrennt.")

