package com.cheese.smartqa.fixture;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;

/** The run's test app: one button that leaves it for the escape package. */
public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Button open = new Button(this);
        open.setText("Open escape");
        open.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                startActivity(new Intent().setComponent(new ComponentName(
                        "com.cheese.smartqa.fixture.escape",
                        "com.cheese.smartqa.fixture.escape.EscapeActivity")));
            }
        });
        setContentView(open);
    }
}
