# Configuración de Supabase para FTMS Bike

La aplicación es estática y se publica en GitHub Pages. La clave publicable de Supabase se usa en el navegador y no es un secreto; la tabla queda protegida por RLS. **Nunca pongas una clave `service_role` ni una clave secreta en este repositorio.**

## Configurar el proyecto

1. Crea un proyecto de Supabase y habilita el proveedor Email en Authentication. El inicio de sesión usa un enlace de un solo uso.
2. En Authentication → URL Configuration, agrega la URL exacta de GitHub Pages a Site URL y a Redirect URLs. Por ejemplo, `https://<usuario>.github.io/<repositorio>/`.
3. La tabla `public.training_sessions` ya está creada en el proyecto remoto bici-stats y su migración está registrada con la versión `20261009163450`. **No vuelvas a aplicar manualmente esa migración en el proyecto remoto.** Para un proyecto nuevo, aplica `supabase/migrations/20261009163450_create_training_sessions.sql` mediante tu flujo normal de migraciones, después de vincular el proyecto y comprobar su historial para confirmar que la tabla aún no existe.
4. Confirma en API Settings que el esquema `public` está expuesto al Data API. La migración concede acceso SQL solo a `authenticated`; RLS sigue limitando las filas por propietario.
5. En `supabase-config.js`, configura el Project URL y la publishable key (o la clave heredada `anon`) del proyecto.
6. Publica desde GitHub Pages por HTTPS. Abre la página con conexión al menos una vez para que se instalen los recursos de la app y el service worker.

## Uso y privacidad local

- Inicia sesión con el mismo correo en tablet y celular. Cada dispositivo conserva una copia en IndexedDB y sincroniza las sesiones propias al recuperar la conexión.
- Las sesiones previas a esta versión permanecen locales y no se asocian automáticamente a una cuenta.
- Las sesiones nuevas se sincronizan solo para la cuenta activa al finalizar. Sin sesión iniciada, el entrenamiento y el historial local siguen funcionando.
- Al cerrar sesión, el historial local de esa cuenta deja de mostrarse en ese dispositivo. Inicia sesión de nuevo para consultarlo.
- Borrar una sesión sin conexión deja una eliminación pendiente, que se envía al reconectar.

El cliente de Supabase JS está fijado a `2.117.3` y se almacena en caché por el service worker. Si el cliente no estaba almacenado antes de quedar sin conexión, la app seguirá grabando localmente y sincronizará al recuperar conexión.
