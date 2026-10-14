# Bot de stock — iPhone 18 Pro Max 256/512 GB (Delaware)

Este bot revisa la disponibilidad de **recogida en tienda** del iPhone 18 Pro Max de 256 GB y de 512 GB, en los cuatro colores, en la Apple Store de Delaware (estado sin impuesto a las ventas):

- **Christiana Mall** (R102) — Newark, DE

Cuando un modelo aparece disponible, avisa por **Telegram**.

- Las consultas salen por tu proxy de Webshare. Si una falla, se reintenta y cada intento sale con otra IP.
- La URL de Apple que usa (`/shop/retail/pickup-message`) y los part numbers se verificaron el 13-oct-2026.
- El bot solo avisa. La compra la haces tú desde el link de la alerta.

## Archivos

| Archivo | Qué es |
|---|---|
| `bot.py` | El bot |
| `config.yaml` | Tiendas, modelos, frecuencia, límite de MB por día |
| `.env` | Token de Telegram, tu chat y el canal (ya migrados del bot viejo) |
| `proxies.txt` | Tu línea de Webshare, **la pegas tú** |
| `monitor.py`, `config.py` | El bot viejo, que se deja intacto como respaldo |

## 1. Pon el proxy

1. En Webshare, abre **Rotating Residential → Endpoint Generator**.
2. En Country elige **United States** y abre la pestaña **HTTP**.
3. Copia la línea, que tiene esta forma:
   ```
   http://USUARIO-rotate:CLAVE@p.webshare.io:80
   ```
4. Pégala en `proxies.txt` y guarda.

## 2. Pruébalo en tu Mac

En la Terminal:

```bash
cd ~/iPhoneBot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python bot.py --test-notify    # te llega un mensaje de prueba a Telegram
python bot.py --test-proxies   # debe salir ✔ con IP de EE.UU. y "Apple HTTP 200"
python bot.py --once           # una ronda real
python bot.py                  # modo continuo (Ctrl+C para parar)
```

## 3. Comandos en Telegram

Los comandos solo los acepta desde tu chat privado.

| Comando | Qué hace |
|---|---|
| `/status` | Estado, último chequeo, modelos disponibles, MB gastados hoy |
| `/pause` | Pausa las consultas, sin gastar proxy |
| `/resume` | Reanuda |
| `/check` | Revisa ahora mismo |

## 4. Consumo del GB de Webshare

- Cada consulta a una tienda pesa unos **14 KB** con los 8 modelos.
- Cada ~2 minutos el bot consulta 1 tienda, lo que da unos **9 MB al día**, o **~270 MB al mes**.
- Cuando aparece stock entra en **cacería agresiva**: revisa cada ~20 segundos durante 10 minutos.
- Si un día pasa de `max_mb_per_day` (30 MB), baja la frecuencia a un tercio hasta el día siguiente y te avisa.
- Con `/status` ves el consumo, y también en el panel de Webshare.

## 5. Dejarlo 24/7 en un VPS (DigitalOcean, USD 4/mes)

1. Crea un Droplet **Ubuntu** con el plan más barato y copia su IP.
2. Desde tu Mac:
   ```bash
   scp -r ~/iPhoneBot root@IP_DEL_DROPLET:/root/
   ssh root@IP_DEL_DROPLET
   cd /root/iPhoneBot && bash deploy/install_vps.sh
   ```
3. Ver logs:
   ```bash
   journalctl -u stockbot -f
   ```
4. Si cambias `config.yaml` o `proxies.txt`, hazlo en `/opt/iphone-stock-bot/` y luego reinicia:
   ```bash
   systemctl restart stockbot
   ```

Importante: **no corras el bot en tu Mac y en el VPS al mismo tiempo.** Gastarías el doble de GB y te llegarían alertas duplicadas.

## Dónde corre ahora: GitHub Actions

El bot corre en el repositorio `camilojimenez0713-glitch/iphone-stock-bot` (workflow **iPhone stock bot**). Cada ejecución dura ~5 h 30 min y lanza ella misma la siguiente.

- `config.yaml` no está en el repositorio: vive en el secreto `CONFIG_YAML`. Si lo cambias, actualízalo con
  `gh secret set CONFIG_YAML --repo camilojimenez0713-glitch/iphone-stock-bot < config.yaml`
  y relanza el bot.
- Para relanzarlo: Actions → iPhone stock bot → Run workflow.
- Para apagarlo: Actions → iPhone stock bot → Disable workflow, y cancela la ejecución en curso.
- **No lo corras a la vez en la Mac o en un VPS.**

## Cambiar modelos o tiendas

En `config.yaml`:

- **Modelos:** `parts` (part number → nombre de la alerta). Para sacar part numbers nuevos, abre la página de compra en apple.com y busca `partNumber` en el código fuente.
- **Tiendas:** `stores` (números `R###`). Para buscar por código postal en vez de por tienda, usa `locations`, pero cada consulta pesa ~100 KB.
