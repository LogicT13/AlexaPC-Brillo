# Brillo Controller — Samsung F24T350FH Software Dimmer

Controlador software de atenuación y modo "Pantalla Negra" para el monitor secundario.

## Estructura de archivos

```
Brillo/
├── brillo_main.py       # Punto de entrada principal
├── overlay_engine.py    # Motor de overlay (tkinter + Win32)
├── control_server.py    # Servidor HTTP local (localhost:52800)
├── hotkey_listener.py   # Atajo de teclado global (Win32 RegisterHotKey)
├── fauxmo.json          # Configuración Fauxmo para Alexa
└── test_brillo.py       # Suite de validación automática
```

## Cómo iniciar

```powershell
cd C:\Users\marti\Documents\AntiG\AlexaPC\Brillo
python brillo_main.py
```

> **REGLA DE SEGURIDAD**: El script NO se configura para inicio automático.
> Reiniciar la ThinkPad siempre restaura el escritorio normal.

## Estados del overlay

| Estado | Nombre    | Opacidad | Descripción                        |
|--------|-----------|----------|------------------------------------|
| 0      | OFF       | 0 %      | Sin overlay — pantalla normal      |
| 1      | DIM       | 60 %     | Overlay negro semitransparente     |
| 2      | BLACKOUT  | 100 %    | Pantalla completamente negra       |

## Controles

### Atajo de teclado global (ThinkPad)

| Atajo              | Acción                            |
|--------------------|-----------------------------------|
| `Ctrl + Alt + B`   | Ciclar: Off → Dim → Blackout → Off|
| `Ctrl + Alt + ESC` | **Pánico**: forzar Off inmediato  |
| `ESC`              | Destruir overlay (dentro del foco)|

### Por voz (Alexa + Fauxmo)

Decir a Alexa:
- **"Alexa, enciende Monitor Secundario"** → Blackout (100 % negro)
- **"Alexa, apaga Monitor Secundario"** → Off (pantalla restaurada)
- **"Alexa, enciende Monitor Atenuado"** → Dim (60 % negro)
- **"Alexa, apaga Monitor Atenuado"** → Off

> Fauxmo expone dos switches virtuales Belkin WeMo en los puertos 52810 y 52811.
> Descubrir dispositivos en la app Alexa: **Dispositivos → Agregar → Buscar**.

## Arquitectura

```
brillo_main.py
    │
    ├─ OverlayEngine (hilo daemon tkinter)
    │      └─ Toplevel WS_EX_LAYERED|WS_EX_TRANSPARENT sobre monitor 1920+0
    │
    ├─ ControlServer (hilo daemon HTTP, localhost:52800)
    │      GET /on      → BLACKOUT
    │      GET /off     → OFF
    │      GET /dim     → DIM
    │      GET /cycle   → ciclar
    │      GET /state   → {"state": "off"|"dim"|"blackout"}
    │
    ├─ HotkeyListener (hilo daemon Win32 message loop)
    │      Ctrl+Alt+B   → cycle()
    │      Ctrl+Alt+ESC → set_state(OFF)
    │
    └─ Fauxmo (subproceso, config: fauxmo.json)
           Puerto 52810 → "Monitor Secundario" → /on / /off
           Puerto 52811 → "Monitor Atenuado"   → /dim / /off
```

## Opciones de línea de comandos

```
python brillo_main.py [--debug] [--no-fauxmo]

  --debug      Activa logging detallado (DEBUG)
  --no-fauxmo  No inicia Fauxmo (útil para depuración)
```

## Validación / Tests

```powershell
# Solo verificación de entorno (sin controller corriendo)
python test_brillo.py --offline

# Suite completa (requiere brillo_main corriendo en otra terminal)
python test_brillo.py
```

## Requisitos

- Python 3.10+ (probado con 3.13.12)
- `fauxmo` 0.8.0+ (`pip install fauxmo`)
- Librería estándar únicamente para el resto (ctypes, tkinter, http.server)
- Windows 10/11 con dos monitores conectados
- La ThinkPad como monitor primario (0,0) y el Samsung F24T350FH como secundario (1920,0)

## Monitor detectado

| Monitor   | Origen | Tamaño    |
|-----------|--------|-----------|
| Primario  | (0,0)  | 1920×1080 |
| Secundario| (1920,0)| 1920×1080 |

## Notas de seguridad

- El overlay usa `WS_EX_TRANSPARENT`: los clics pasan al escritorio en modo DIM.
- En BLACKOUT el overlay **no** es transparente a clics (pantalla totalmente negra).
- Los procesos de fondo (reproductores de audio/video) continúan intactos en cualquier estado.
- Sin autostart: ninguna entrada en Startup ni en Task Scheduler.
