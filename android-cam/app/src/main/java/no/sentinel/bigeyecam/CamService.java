package no.sentinel.bigeyecam;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.SurfaceTexture;
import android.graphics.YuvImage;
import android.hardware.Camera;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

/**
 * Turns the phone into a BigEye camera: the back camera's picture as MJPEG over plain HTTP on the local network.
 *
 *   /cam.mjpg      live picture (multipart/x-mixed-replace), what BigEye reads
 *   /snapshot.jpg  the newest picture
 *   /status        JSON: battery, charging, battery temperature, size, fps, uptime, and "turn": how many degrees
 *                  clockwise the picture must be turned to stand upright (from the phone's gravity sensor)
 *   /torch?on=1|0  the phone's light
 *   /config?size=1280x720&fps=10&quality=70&camera=0|1   (saved, camera restarts)
 *
 * No cloud, no account, no ads: it only answers on the network the phone is on (BigEye puts it on the camera Wi-Fi,
 * which has no way out).
 */
public class CamService extends Service implements SensorEventListener {
    static final int PORT = 8080;
    static final String CH = "cam";
    static volatile boolean running;

    private final Object frameLock = new Object();
    private volatile byte[] jpeg;
    private volatile long frameNo, frameTime;
    private long started;
    private Camera camera;
    private HandlerThread camThread;
    private Handler camHandler;
    private SurfaceTexture texture;
    private ServerSocket server;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private int width, height;
    private long lastEncode;
    private volatile boolean torch;
    private SharedPreferences prefs;
    private SensorManager sensors;
    private volatile int sensorDeg = 90;      // the camera chip's own turn (Camera.CameraInfo.orientation)
    private volatile boolean frontCam;
    private volatile int holdDeg = -1;        // how the phone is held: 0 upright, 90 turned clockwise, 180, 270
    private int candDeg = -1, candCount;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("cam", MODE_PRIVATE);
        started = System.currentTimeMillis();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) {
            prefs.edit().putBoolean("on", false).apply();
            stopSelf();
            return START_NOT_STICKY;
        }
        prefs.edit().putBoolean("on", true).apply();
        Notification n = notification();
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA);
            else startForeground(1, n);
        } catch (RuntimeException e) {
            // Android 14+: a camera service may only start while the app is on screen (after an update or a restart
            // it is not). Ask the owner to open the app instead of crashing.
            BootReceiver.askToOpen(this);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!running) {
            running = true;
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bigeyecam:run");
            wake.acquire();
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "bigeyecam:wifi");
            wifi.acquire();
            camThread = new HandlerThread("camera");
            camThread.start();
            camHandler = new Handler(camThread.getLooper());
            camHandler.post(this::openCamera);
            new Thread(this::serve, "http").start();
            sensors = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
            Sensor acc = sensors == null ? null : sensors.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
            if (acc != null) sensors.registerListener(this, acc, SensorManager.SENSOR_DELAY_NORMAL, camHandler);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (sensors != null) sensors.unregisterListener(this);
        try { if (server != null) server.close(); } catch (IOException ignored) { }
        if (camHandler != null) camHandler.post(this::closeCamera);
        if (camThread != null) camThread.quitSafely();
        if (wake != null && wake.isHeld()) wake.release();
        if (wifi != null && wifi.isHeld()) wifi.release();
        super.onDestroy();
    }

    private Notification notification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(CH, "Camera running", NotificationManager.IMPORTANCE_LOW));
            b = new Notification.Builder(this, CH);
        } else {
            b = new Notification.Builder(this);
        }
        int pf = Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0;
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), pf);
        return b.setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("BigEye Cam is filming")
                .setContentText("Picture for BigEye on port " + PORT)
                .setContentIntent(open)
                .setOngoing(true)
                .build();
    }

    // ── camera ────────────────────────────────────────────────────────────
    @SuppressWarnings("deprecation")  // Camera1: the one API that works the same from Android 5 to today
    private void openCamera() {
        try {
            int id = Math.min(prefs.getInt("camera", 0), Math.max(0, Camera.getNumberOfCameras() - 1));
            camera = Camera.open(id);
            Camera.CameraInfo info = new Camera.CameraInfo();
            Camera.getCameraInfo(id, info);
            sensorDeg = info.orientation;
            frontCam = info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT;
            Camera.Parameters p = camera.getParameters();
            Camera.Size s = pick(p.getSupportedPreviewSizes(), prefs.getString("size", "1280x720"));
            p.setPreviewSize(s.width, s.height);
            p.setPreviewFormat(ImageFormat.NV21);
            List<String> focus = p.getSupportedFocusModes();
            if (focus != null && focus.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO))
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO);
            if (p.isVideoStabilizationSupported()) p.setVideoStabilization(false);
            // The chip itself should not run faster than needed: at 30 fps it works (and heats) for pictures we drop.
            // Pick the slowest range that still covers the asked frame rate (A15: 15-30 -> 15-15, about half the work).
            List<int[]> ranges = p.getSupportedPreviewFpsRange();
            if (ranges != null) {
                int want = Math.max(1, prefs.getInt("fps", 10)) * 1000;
                int[] best = null;
                for (int[] r : ranges) {
                    if (r[Camera.Parameters.PREVIEW_FPS_MAX_INDEX] < want) continue;
                    if (best == null || r[Camera.Parameters.PREVIEW_FPS_MAX_INDEX] < best[Camera.Parameters.PREVIEW_FPS_MAX_INDEX]
                            || (r[Camera.Parameters.PREVIEW_FPS_MAX_INDEX] == best[Camera.Parameters.PREVIEW_FPS_MAX_INDEX]
                                && r[Camera.Parameters.PREVIEW_FPS_MIN_INDEX] < best[Camera.Parameters.PREVIEW_FPS_MIN_INDEX])) best = r;
                }
                if (best != null) p.setPreviewFpsRange(best[Camera.Parameters.PREVIEW_FPS_MIN_INDEX], best[Camera.Parameters.PREVIEW_FPS_MAX_INDEX]);
            }
            List<String> flash = p.getSupportedFlashModes();   // light: exactly what was asked for, never left on by a restart
            if (flash != null && flash.contains(Camera.Parameters.FLASH_MODE_TORCH) && torch) p.setFlashMode(Camera.Parameters.FLASH_MODE_TORCH);
            else if (flash != null && flash.contains(Camera.Parameters.FLASH_MODE_OFF)) p.setFlashMode(Camera.Parameters.FLASH_MODE_OFF);
            camera.setParameters(p);
            width = s.width;
            height = s.height;
            texture = new SurfaceTexture(10);   // a preview needs a target; nothing is shown on screen
            camera.setPreviewTexture(texture);
            int bytes = width * height * ImageFormat.getBitsPerPixel(ImageFormat.NV21) / 8;
            for (int i = 0; i < 3; i++) camera.addCallbackBuffer(new byte[bytes]);
            camera.setPreviewCallbackWithBuffer(this::onFrame);
            camera.startPreview();
        } catch (Exception e) {
            closeCamera();
            if (running) camHandler.postDelayed(this::openCamera, 5000);   // busy or failed: try again
        }
    }

    @SuppressWarnings("deprecation")
    private void closeCamera() {
        if (camera != null) {
            try { camera.setPreviewCallbackWithBuffer(null); camera.stopPreview(); } catch (Exception ignored) { }
            camera.release();
            camera = null;
        }
        if (texture != null) { texture.release(); texture = null; }
    }

    @SuppressWarnings("deprecation")
    private static Camera.Size pick(List<Camera.Size> sizes, String want) {
        int ww = 1280, wh = 720;
        try { String[] p = want.split("x"); ww = Integer.parseInt(p[0]); wh = Integer.parseInt(p[1]); } catch (Exception ignored) { }
        Camera.Size best = sizes.get(0);
        long bestD = Long.MAX_VALUE;
        for (Camera.Size s : sizes) {
            long d = Math.abs((long) s.width * s.height - (long) ww * wh) + Math.abs(s.width * wh - s.height * ww);
            if (d < bestD) { best = s; bestD = d; }
        }
        return best;
    }

    @SuppressWarnings("deprecation")
    private void onFrame(byte[] data, Camera cam) {
        long now = System.currentTimeMillis();
        int fps = Math.max(1, Math.min(30, prefs.getInt("fps", 10)));
        if (now - lastEncode >= 1000 / fps) {
            lastEncode = now;
            ByteArrayOutputStream out = new ByteArrayOutputStream(width * height / 4);
            new YuvImage(data, ImageFormat.NV21, width, height, null)
                    .compressToJpeg(new Rect(0, 0, width, height), Math.max(30, Math.min(95, prefs.getInt("quality", 70))), out);
            synchronized (frameLock) {
                jpeg = out.toByteArray();
                frameNo++;
                frameTime = now;
                frameLock.notifyAll();
            }
        }
        cam.addCallbackBuffer(data);
    }

    @SuppressWarnings("deprecation")
    private void setTorch(boolean on) {
        torch = on;
        camHandler.post(() -> {
            if (camera == null) return;
            try {
                Camera.Parameters p = camera.getParameters();
                if (p.getSupportedFlashModes() == null || !p.getSupportedFlashModes().contains(Camera.Parameters.FLASH_MODE_TORCH)) return;
                p.setFlashMode(on ? Camera.Parameters.FLASH_MODE_TORCH : Camera.Parameters.FLASH_MODE_OFF);
                camera.setParameters(p);
            } catch (Exception ignored) { }
        });
    }

    // ── tiny web server ───────────────────────────────────────────────────
    private void serve() {
        while (running) {
            try (ServerSocket s = new ServerSocket(PORT)) {
                server = s;
                while (running) {
                    Socket c = s.accept();
                    new Thread(() -> {
                        try { handle(c); } catch (Throwable ignored) { }   // one bad request never takes the camera down
                    }, "client").start();
                }
            } catch (IOException e) {
                if (running) try { Thread.sleep(2000); } catch (InterruptedException ignored) { }
            }
        }
    }

    private void handle(Socket c) {
        try (Socket sock = c) {
            sock.setSoTimeout(15000);
            BufferedReader in = new BufferedReader(new InputStreamReader(sock.getInputStream(), StandardCharsets.ISO_8859_1));
            String line = in.readLine();
            if (line == null) return;
            while (true) { String h = in.readLine(); if (h == null || h.isEmpty()) break; }
            String[] parts = line.split(" ");
            String target = parts.length > 1 ? parts[1] : "/";
            String path = target.split("\\?")[0];
            String query = target.contains("?") ? target.substring(target.indexOf('?') + 1) : "";
            OutputStream out = sock.getOutputStream();
            switch (path) {
                case "/cam.mjpg": stream(sock, out); break;
                case "/snapshot.jpg": {
                    byte[] j = jpeg;
                    if (j == null) { text(out, 503, "no picture yet"); break; }
                    out.write(("HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nCache-Control: no-store\r\nContent-Length: "
                            + j.length + "\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
                    out.write(j);
                    break;
                }
                case "/status": json(out, status()); break;
                case "/torch": {
                    boolean on = param(query, "on", "0").equals("1");
                    setTorch(on);
                    json(out, "{\"torch\":" + on + "}");
                    break;
                }
                case "/config": {
                    SharedPreferences.Editor e = prefs.edit();
                    String size = param(query, "size", null), fps = param(query, "fps", null), q = param(query, "quality", null),
                            cam = param(query, "camera", null);
                    if (size != null && size.matches("\\d{3,4}x\\d{3,4}")) e.putString("size", size);
                    if (fps != null && fps.matches("\\d{1,2}")) e.putInt("fps", Integer.parseInt(fps));
                    if (q != null && q.matches("\\d{2}")) e.putInt("quality", Integer.parseInt(q));
                    if (cam != null && cam.matches("[01]")) e.putInt("camera", Integer.parseInt(cam));
                    e.apply();
                    if (size != null || cam != null) camHandler.post(() -> { closeCamera(); openCamera(); });
                    json(out, status());
                    break;
                }
                default: text(out, 404, "BigEye Cam: /cam.mjpg /snapshot.jpg /status /torch?on=1 /config?size=1280x720&fps=10");
            }
            out.flush();
        } catch (IOException ignored) { }
    }

    private void stream(Socket sock, OutputStream out) throws IOException {
        sock.setSoTimeout(0);
        out.write(("HTTP/1.0 200 OK\r\nCache-Control: no-store\r\nConnection: close\r\n"
                + "Content-Type: multipart/x-mixed-replace; boundary=bigeyecam\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        long seen = -1;
        while (running) {
            byte[] j;
            synchronized (frameLock) {
                long until = System.currentTimeMillis() + 5000;
                while (frameNo == seen && running) {
                    long left = until - System.currentTimeMillis();
                    if (left <= 0) break;
                    try { frameLock.wait(left); } catch (InterruptedException e) { return; }
                }
                if (frameNo == seen || jpeg == null) continue;   // no (new) picture yet: keep waiting (camera opening)
                seen = frameNo;
                j = jpeg;
            }
            out.write(("--bigeyecam\r\nContent-Type: image/jpeg\r\nContent-Length: " + j.length + "\r\n\r\n")
                    .getBytes(StandardCharsets.ISO_8859_1));
            out.write(j);
            out.write("\r\n".getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
        }
    }

    /** Gravity tells how the phone is held; a new way must hold for ~2 s before it counts (no flapping). */
    @Override
    public void onSensorChanged(SensorEvent e) {
        float x = e.values[0], y = e.values[1], z = e.values[2];
        if (Math.abs(z) > 8.0f) return;                 // lying flat: keep what we had
        int deg = Math.abs(y) >= Math.abs(x) ? (y > 0 ? 0 : 180) : (x < 0 ? 90 : 270);
        if (Math.max(Math.abs(x), Math.abs(y)) < 4.0f) return;
        if (deg == candDeg) candCount++; else { candDeg = deg; candCount = 1; }
        if (candCount >= 10) holdDeg = deg;
    }

    @Override
    public void onAccuracyChanged(Sensor sensor, int accuracy) { }

    /** Degrees clockwise the picture must be turned to stand upright, or -1 when not known yet. */
    private int turn() {
        int h = holdDeg;
        if (h < 0) return -1;
        return frontCam ? (sensorDeg - h + 360) % 360 : (sensorDeg + h) % 360;
    }

    private String status() {
        Intent b = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        int level = b == null ? -1 : b.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = b == null ? 100 : b.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        int plugged = b == null ? 0 : b.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        int temp = b == null ? 0 : b.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0);
        int health = b == null ? 0 : b.getIntExtra(BatteryManager.EXTRA_HEALTH, 0);
        long age = frameTime == 0 ? -1 : System.currentTimeMillis() - frameTime;
        return String.format(Locale.ROOT,
                "{\"app\":\"BigEye Cam\",\"version\":\"1.0.5\",\"model\":\"%s\",\"android\":\"%s\",\"battery_pct\":%d,"
                        + "\"charging\":%b,\"battery_temp_c\":%.1f,\"battery_health\":%d,\"width\":%d,\"height\":%d,"
                        + "\"fps\":%d,\"quality\":%d,\"camera\":%d,\"torch\":%b,\"frame_age_ms\":%d,\"uptime_s\":%d,\"turn\":%d}",
                Build.MODEL.replace("\"", ""), Build.VERSION.RELEASE, scale > 0 ? level * 100 / scale : level,
                plugged != 0, temp / 10.0, health, width, height, prefs.getInt("fps", 10), prefs.getInt("quality", 70),
                prefs.getInt("camera", 0), torch, age, (System.currentTimeMillis() - started) / 1000, turn());
    }

    private static String param(String query, String key, String def) {
        for (String kv : query.split("&")) {
            int i = kv.indexOf('=');
            if (i > 0 && kv.substring(0, i).equals(key)) return kv.substring(i + 1);
        }
        return def;
    }

    private static void json(OutputStream out, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        out.write(("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: "
                + b.length + "\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        out.write(b);
    }

    private static void text(OutputStream out, int code, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        out.write(("HTTP/1.0 " + code + " X\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: " + b.length
                + "\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        out.write(b);
    }
}
