# Control Flip Flat

**Versión 1.0.0** · Aplicación de escritorio para Linux (Ubuntu) · Python 3 / Tkinter / INDI

Control Flip Flat es una interfaz gráfica para manejar la iluminación de un panel **Optec/Alnitak Flip Flat** mediante el driver **`indi_flipflat`** del ecosistema INDI. Permite gestionar el servidor, conectarse al dispositivo, encender y apagar la iluminación y ajustar el brillo sin utilizar manualmente la terminal.

> Esta publicación **1.0.0** corresponde al programa `control_flipflat_v8_3_puerto.py`. La numeración 1.0.0 identifica la versión del repositorio; no modifica el protocolo ni la versión del driver INDI.

## Características

- **Interfaz redimensionable**, con controles habituales de minimizar y maximizar de Linux.
- **Modo nocturno rojo**, activable y desactivable desde la interfaz, con fondo oscuro y controles adaptados para observación astronómica.
- **Servidor INDI:** detecta un servidor existente en `127.0.0.1:7624` o inicia uno con `indi_flipflat`. Al finalizar, solo detiene el servidor si lo inició la propia aplicación.
- **Puerto serie configurable:** campo editable para seleccionar una ruta como `/dev/ttyUSB0` o `/dev/serial/by-id/...` antes de conectar.
- **Conexión al dispositivo** identificado en INDI como `Flip Flat`.
- **Encendido y apagado independientes** de la iluminación, manteniendo la conexión cuando se usa «Apagar luz».
- **Indicador LED de estado** basado en la información publicada por INDI; distingue encendido, apagado y estado no confirmado.
- **Brillo de 0 a 255**, ajustable mediante botones `+` / `−` o un deslizador. Muestra el valor y consulta su confirmación al servidor.
- **Actualización manual** del estado del panel.
- **Finalización de sesión:** intenta apagar la iluminación, desconectar el dispositivo y detener el servidor iniciado por el programa.
- **Registro de actividad** en pantalla y en un archivo rotativo para facilitar el diagnóstico.
- **Operaciones INDI en segundo plano**, evitando bloquear la ventana durante las consultas y cambios de estado.

## Requisitos

- Linux con entorno gráfico (desarrollado para Ubuntu).
- Python 3 con **Tkinter**.
- Servidor y utilidades INDI: `indiserver`, `indi_getprop`, `indi_setprop`.
- Driver INDI **`indi_flipflat`**.
- Panel compatible conectado por USB/serie y permisos de acceso al dispositivo.

En sistemas Ubuntu con paquetes disponibles, puedes instalar las dependencias básicas mediante:

```bash
sudo apt update
sudo apt install python3 python3-tk indi-bin
```

El paquete que proporciona `indi_flipflat` puede variar según la distribución o los repositorios INDI instalados. Comprueba la disponibilidad de los ejecutables:

```bash
which python3 indiserver indi_flipflat indi_getprop indi_setprop
```

## Instalación y ejecución

1. Descarga o clona el repositorio.
2. Sitúate en el directorio que contiene el script.
3. Ejecuta:

```bash
python3 control_flipflat_v8_3_puerto.py
```

No se necesitan paquetes Python externos para la interfaz y la comunicación: se emplean la biblioteca estándar y las utilidades INDI instaladas en el sistema.

## Uso

1. Conecta y alimenta el panel Flat Flat mediante USB/serie.
2. Pulsa **Iniciar servidor**. Si ya existe un servidor INDI en el puerto 7624, el programa intentará reutilizarlo.
3. Introduce el **puerto serie** adecuado en el apartado del dispositivo.
4. Pulsa **Conectar** y espera la confirmación.
5. Utiliza **Encender** y **Apagar luz** para controlar la iluminación.
6. Ajusta el brillo con los botones `+` / `−` o el deslizador (rango 0–255).
7. Pulsa **Actualizar estado** si quieres consultar de nuevo el valor y estado reportados por INDI.
8. Al terminar, utiliza **Finalizar sesión** para apagar, desconectar y cerrar el servidor propio.

### Puerto serie

El valor inicial configurado en el código es:

```text
/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

Puedes cambiarlo en la interfaz **antes de conectar**. Otras rutas habituales son `/dev/ttyUSB0` o `/dev/serial/by-id/...`. La ruta elegida se utiliza para configurar la propiedad INDI `DEVICE_PORT.PORT` cuando el dispositivo todavía no está conectado.

> **Nota:** el puerto seleccionado no se guarda entre ejecuciones en esta versión. Si el dispositivo ya está conectado a un servidor INDI externo, el programa reutiliza esa conexión y no cambia automáticamente su puerto.

### Modo nocturno

Activa la casilla **Modo nocturno (rojo)** para cambiar la paleta visual sin modificar la iluminación física del panel. Puedes alternar de modo durante una sesión.

## Comunicación INDI

El programa utiliza `indi_getprop` e `indi_setprop` y las siguientes propiedades del dispositivo `Flip Flat`:

| Función | Propiedad |
| --- | --- |
| Conectar | `CONNECTION.CONNECT` |
| Desconectar | `CONNECTION.DISCONNECT` |
| Puerto serie | `DEVICE_PORT.PORT` |
| Encender luz | `FLAT_LIGHT_CONTROL.FLAT_LIGHT_ON` |
| Apagar luz | `FLAT_LIGHT_CONTROL.FLAT_LIGHT_OFF` |
| Brillo | `FLAT_LIGHT_INTENSITY.FLAT_LIGHT_INTENSITY_VALUE` |
| Estado de luz | `Status.Light` |

El servidor INDI está configurado en el código para escuchar en `127.0.0.1:7624`.

## Registro de actividad

Los eventos y errores se muestran en la ventana y se guardan en:

```text
~/.local/state/control_flipflat/control_flipflat.log
```

El registro utiliza rotación de archivos para limitar su tamaño.

## Resolución de problemas

**No se puede iniciar INDI:** comprueba que `indiserver` e `indi_flipflat` están instalados y que el puerto TCP 7624 no está ocupado por un proceso incompatible.

**No conecta el panel:** verifica el cable USB, la alimentación, los permisos del puerto serie y la ruta indicada en el campo **Puerto serie**. Puedes consultar los puertos disponibles con:

```bash
ls -l /dev/serial/by-id/
ls -l /dev/ttyUSB*
```

**No se encuentran propiedades o falla el brillo:** asegúrate de que el dispositivo está conectado y de que el driver publica las propiedades esperadas:

```bash
indi_getprop -h 127.0.0.1 -p 7624 'Flip Flat.*.*'
```

**Errores o tiempos de espera:** revisa el registro de actividad. Las consultas INDI tienen tiempos de espera y reintentos; su funcionamiento depende también de la versión del driver y del estado físico del dispositivo.

## Limitaciones de la versión 1.0.0

- La configuración del puerto y la selección del modo nocturno **no se guardan** automáticamente en un archivo de preferencias.
- El programa no adquiere imágenes de cámara ni calcula exposiciones automáticas para flats.
- La comunicación se realiza mediante utilidades INDI de línea de comandos, no mediante una conexión INDI persistente propia.
- El programa se ha diseñado para el dispositivo publicado como **`Flip Flat`** y las propiedades indicadas; otros controladores pueden requerir ajustes.

## Estado del proyecto

Versión **1.0.0**: primera versión para publicación en GitHub, basada en la evolución de la aplicación hasta la interfaz con modo nocturno y puerto serie configurable.

La comunicación y el control del hardware deben verificarse con el modelo concreto y la instalación INDI utilizada.

## Licencia

**Pendiente de definir.** Antes de publicar el repositorio, añade un archivo `LICENSE` con la licencia que desees utilizar.
