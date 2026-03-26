# Handoff — IBKR Dashboard

## Contexto de esta sesión (mac puertecito → mac valenciana)

En puertecito solo se reactivó la API de TWS (había sido desactivada por una
actualización de seguridad). No se hicieron cambios de código.

---

## Cómo correr el proyecto aquí

1. **Habilitar API en TWS**
   - Abre TWS
   - Ve a `Configuration > API > Settings`
   - Marca ✓ `Enable ActiveX and Socket Clients`
   - Haz clic en **Apply** y luego **OK**
   - Si aparece el mensaje "API Security Settings Updated", haz clic en OK

2. **Arrancar el server**
   ```bash
   cd ~/Downloads/ibkr
   python3 ibkr_server.py
   ```

3. **Espera ~5 segundos** antes de abrir el dashboard (el server necesita
   ese tiempo para conectarse a TWS). Si `/status` devuelve `connected: false`,
   es normal — no reinicies, solo espera.

4. **Abrir el dashboard**
   - Abre `dashboard.html` directamente en el browser como archivo local
   - NO uses `http://localhost:8766/dashboard.html` (el server no sirve el HTML, da 404)
   - Usa: `Archivo > Abrir` en el browser o arrastra el archivo

---

## Una vez que todo funcione, borra este archivo.
