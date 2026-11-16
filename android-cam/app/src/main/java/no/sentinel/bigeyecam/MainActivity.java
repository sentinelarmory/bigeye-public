package no.sentinel.bigeyecam;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.Locale;

/** One screen: the address BigEye reads, and Start / Stop. The camera keeps running with the screen off. */
public class MainActivity extends Activity {
    private TextView info;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(Color.rgb(14, 26, 36));
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("BigEye Cam");
        title.setTextColor(Color.WHITE);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        info = new TextView(this);
        info.setTextColor(Color.rgb(170, 190, 205));
        info.setTextSize(16);
        info.setGravity(Gravity.CENTER);
        info.setPadding(0, pad / 2, 0, pad / 2);
        Button start = new Button(this);
        start.setText("Start camera");
        start.setOnClickListener(v -> startCam());
        Button stop = new Button(this);
        stop.setText("Stop camera");
        stop.setOnClickListener(v -> {
            startService(new Intent(this, CamService.class).setAction("stop"));
            show();
        });
        box.addView(title);
        box.addView(info);
        box.addView(start);
        box.addView(stop);
        setContentView(box);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        if (Build.VERSION.SDK_INT >= 23) {
            String[] want = Build.VERSION.SDK_INT >= 33
                    ? new String[]{Manifest.permission.CAMERA, Manifest.permission.POST_NOTIFICATIONS}
                    : new String[]{Manifest.permission.CAMERA};
            if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) requestPermissions(want, 1);
            else startCam();
        } else {
            startCam();
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) startCam();
        else show();
    }

    @Override
    protected void onResume() {
        super.onResume();
        show();
    }

    private void startCam() {
        Intent i = new Intent(this, CamService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
        else startService(i);
        info.postDelayed(this::show, 800);
    }

    @SuppressWarnings("deprecation")
    private void show() {
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        int ip = wm.getConnectionInfo().getIpAddress();
        String addr = ip == 0 ? "(no Wi-Fi)" : String.format(Locale.ROOT, "%d.%d.%d.%d", ip & 255, ip >> 8 & 255, ip >> 16 & 255, ip >> 24 & 255);
        info.setText((CamService.running ? "Filming.\n\n" : "Stopped.\n\n")
                + "BigEye address:\nhttp://" + addr + ":" + CamService.PORT + "/cam.mjpg\n\n"
                + "You can turn the screen off; the camera keeps running.");
    }
}
