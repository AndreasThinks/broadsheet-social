package com.andreasthinks.crossposter

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Message
import android.util.Log
import android.view.Gravity
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import kotlin.concurrent.thread

/** The app's one screen: a WebView onto the Python server running in this process. */
class MainActivity : ComponentActivity() {
    companion object {
        const val EXTRA_PATH = "path"
    }

    private val base = "http://127.0.0.1:$PORT"
    private lateinit var web: WebView
    private lateinit var status: TextView
    private var ready = false
    private var pendingPath: String? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        fileCallback = null
    }
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        status = TextView(this).apply { text = getString(R.string.starting); gravity = Gravity.CENTER; setTextColor(Color.GRAY) }
        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#f7f7fb"))
            addView(web)
            addView(status)
        }
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }
        setContentView(root)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            setSupportMultipleWindows(true)
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (url.scheme == "http" || url.scheme == "https") return false  // sign-in pages stay in the app
                openExternally(url)
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                status.visibility = android.view.View.GONE
                pendingPath?.let { pendingPath = null; view.loadUrl(base + it) }
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            // Links marked target=_blank (posts, profiles, link cards) open in the browser.
            override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: Message): Boolean {
                val catcher = WebView(this@MainActivity)
                catcher.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                        openExternally(request.url)
                        v.destroy()
                        return true
                    }
                }
                (resultMsg.obj as WebView.WebViewTransport).webView = catcher
                resultMsg.sendToTarget()
                return true
            }

            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    pickFiles.launch(params.createIntent()); true
                } catch (e: Exception) {
                    fileCallback = null; false
                }
            }
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        pendingPath = intent.getStringExtra(EXTRA_PATH)
        if (Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)

        thread(name = "start-python") {
            try {
                val token = startPython(this).getModule("crossposter_android")
                    .callAttr("start", filesDir.absolutePath, PORT, Notifier(this)).toString()
                runOnUiThread {
                    ready = true
                    web.loadUrl("$base/login/desktop?token=" + Uri.encode(token))
                }
            } catch (e: Exception) {
                Log.e("crossposter", "start failed", e)
                runOnUiThread { status.text = "Cross-poster couldn't start:\n${e.message}" }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val path = intent.getStringExtra(EXTRA_PATH) ?: return
        if (ready) web.loadUrl(base + path) else pendingPath = path
    }

    private fun openExternally(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (e: Exception) {
            Log.w("crossposter", "no app for $uri")
        }
    }
}
