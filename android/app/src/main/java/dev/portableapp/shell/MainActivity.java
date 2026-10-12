package dev.portableapp.shell;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.ByteArrayInputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    static final int WEB_VIEW_ID = 0x1020abc;
    private static final int OPEN_DOCUMENT = 501;
    private static final int CREATE_DOCUMENT = 502;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final List<SafeFiles.StagedFile> imports = new ArrayList<>();
    private WebView webView;
    private TextView status;
    private Button retry;
    // Resource interception runs on WebView's background request threads.
    private volatile PortableRuntime.Session session;
    private ValueCallback<Uri[]> fileCallback;
    private boolean csvAllowed;
    private boolean jsonAllowed;
    private boolean fileBusy;
    private boolean pageError;
    private boolean destroyed;
    private SafeFiles.StagedFile pendingExport;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        PortableRuntime.attach();
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        status = new TextView(this);
        status.setPadding(24, 16, 24, 16);
        status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        root.addView(status);
        retry = new Button(this);
        retry.setText(R.string.retry);
        retry.setVisibility(View.GONE);
        retry.setOnClickListener(view -> start());
        root.addView(retry);
        webView = new WebView(this);
        webView.setId(WEB_VIEW_ID);
        root.addView(webView, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);
        configureWebView();
        start();
    }

    private void start() {
        retry.setVisibility(View.GONE);
        message(R.string.starting);
        pageError = false;
        worker.execute(() -> {
            try {
                PortableRuntime.Session ready = PortableRuntime.obtain(getApplicationContext());
                runOnUiThread(() -> {
                    if (destroyed) return;
                    session = ready;
                    CookieManager cookies = CookieManager.getInstance();
                    cookies.setAcceptCookie(true);
                    cookies.setAcceptThirdPartyCookies(webView, false);
                    cookies.removeAllCookies(removed -> cookies.setCookie(session.origin, session.cookie, accepted -> {
                        if (destroyed) return;
                        if (Boolean.TRUE.equals(accepted)) webView.loadUrl(session.origin + "/");
                        else error(R.string.start_failed);
                    }));
                });
            } catch (Exception failure) {
                runOnUiThread(() -> { if (!destroyed) error(R.string.start_failed); });
            }
        });
    }

    @SuppressWarnings("deprecation")
    private void configureWebView() {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        // Needed only for bounded copies returned by the file-input callback.
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setGeolocationEnabled(false);
        ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) { return blocked(); }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (session == null || !session.policy.isInternal(url)) { message(R.string.navigation_blocked); return true; }
                if (session.policy.isDownload(url)) { export(url); return true; }
                return false;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                PortableRuntime.Session current = session;
                return current != null && current.policy.isInternal(request.getUrl().toString()) ? null : blocked();
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (!pageError && session != null && session.policy.isInternal(url)) {
                    status.setVisibility(View.GONE);
                    retry.setVisibility(View.GONE);
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError issue) {
                if (request.isForMainFrame()) { pageError = true; error(R.string.page_failed); }
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) { pageError = true; error(R.string.page_failed); }
            }
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                // Restart the Activity to replace the unusable renderer; the private server survives.
                view.destroy();
                error(R.string.webview_failed);
                retry.setOnClickListener(button -> recreate());
                return true;
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public void onPermissionRequest(PermissionRequest request) { request.deny(); }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileBusy || session == null || !session.policy.isInternal(view.getUrl()) || params.getMode() != FileChooserParams.MODE_OPEN) {
                    callback.onReceiveValue(null);
                    if (fileBusy) message(R.string.operation_busy);
                    return true;
                }
                csvAllowed = false; jsonAllowed = false;
                String[] accepts = params.getAcceptTypes();
                for (String accepted : accepts) {
                    if (accepted.contains("csv")) csvAllowed = true;
                    if (accepted.contains("json")) jsonAllowed = true;
                    if (accepted.isEmpty() || accepted.equals("*/*")) { csvAllowed = true; jsonAllowed = true; }
                }
                if (accepts.length == 0) { csvAllowed = true; jsonAllowed = true; }
                if (!csvAllowed && !jsonAllowed) { callback.onReceiveValue(null); message(R.string.file_read_failed); return true; }
                fileCallback = callback;
                fileBusy = true;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
                List<String> types = new ArrayList<>();
                if (csvAllowed) { types.add("text/csv"); types.add("text/comma-separated-values"); types.add("text/plain"); }
                if (jsonAllowed) types.add("application/json");
                types.add("application/octet-stream");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, types.toArray(new String[0]));
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false);
                intent.putExtra(Intent.EXTRA_LOCAL_ONLY, true);
                try { startActivityForResult(intent, OPEN_DOCUMENT); }
                catch (ActivityNotFoundException failure) { finishImport(null); message(R.string.file_read_failed); }
                return true;
            }
        });
        webView.setDownloadListener((url, userAgent, disposition, mime, length) -> export(url));
    }

    private static WebResourceResponse blocked() {
        return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden",
                Collections.singletonMap("Cache-Control", "no-store"), new ByteArrayInputStream(new byte[0]));
    }

    private void export(String url) {
        if (session == null || !session.policy.isDownload(url)) { message(R.string.navigation_blocked); return; }
        if (fileBusy) { message(R.string.operation_busy); return; }
        fileBusy = true;
        message(R.string.export_preparing);
        worker.execute(() -> {
            try {
                SafeFiles.StagedFile staged = SafeFiles.download(getApplicationContext(), session, url);
                runOnUiThread(() -> {
                    if (destroyed) { staged.file.delete(); return; }
                    pendingExport = staged;
                    Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                            .setType(staged.mime).putExtra(Intent.EXTRA_TITLE, staged.name).putExtra(Intent.EXTRA_LOCAL_ONLY, true);
                    try { startActivityForResult(intent, CREATE_DOCUMENT); }
                    catch (ActivityNotFoundException failure) { clearExport(); message(R.string.export_failed); }
                });
            } catch (Exception failure) {
                runOnUiThread(() -> { if (!destroyed) { fileBusy = false; message(R.string.export_failed); } });
            }
        });
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == OPEN_DOCUMENT) {
            if (result != RESULT_OK || data == null || data.getData() == null) { finishImport(null); message(R.string.file_cancelled); return; }
            Uri uri = data.getData();
            message(R.string.file_reading);
            worker.execute(() -> {
                try {
                    SafeFiles.StagedFile staged = SafeFiles.importDocument(getApplicationContext(), uri, csvAllowed, jsonAllowed);
                    runOnUiThread(() -> {
                        if (destroyed) { SafeFiles.removeImport(staged); return; }
                        imports.add(staged);
                        finishImport(new Uri[]{staged.uri(this)});
                        status.setVisibility(View.GONE);
                    });
                } catch (Exception failure) {
                    int reason = failure instanceof BoundaryPolicy.SizeLimitException ? R.string.file_too_large : R.string.file_read_failed;
                    runOnUiThread(() -> { if (!destroyed) { finishImport(null); message(reason); } });
                }
            });
        } else if (request == CREATE_DOCUMENT) {
            SafeFiles.StagedFile staged = pendingExport;
            if (staged == null) { fileBusy = false; return; }
            if (result != RESULT_OK || data == null || data.getData() == null) { clearExport(); message(R.string.export_cancelled); return; }
            Uri destination = data.getData();
            worker.execute(() -> {
                int notice = R.string.export_saved;
                try { SafeFiles.saveDocument(getApplicationContext(), staged, destination); }
                catch (Exception failure) { notice = R.string.export_failed; }
                finally { staged.file.delete(); }
                int finalNotice = notice;
                runOnUiThread(() -> { if (!destroyed) { pendingExport = null; fileBusy = false; message(finalNotice); } });
            });
        }
    }

    private void finishImport(Uri[] uris) {
        ValueCallback<Uri[]> callback = fileCallback;
        fileCallback = null;
        fileBusy = false;
        if (callback != null) callback.onReceiveValue(uris);
    }
    private void clearExport() {
        if (pendingExport != null) pendingExport.file.delete();
        pendingExport = null;
        fileBusy = false;
    }
    private void message(int text) { status.setText(text); status.setVisibility(View.VISIBLE); }
    private void error(int text) { message(text); retry.setVisibility(View.VISIBLE); }

    @Override protected void onDestroy() {
        destroyed = true;
        finishImport(null);
        clearExport();
        worker.shutdownNow();
        for (SafeFiles.StagedFile file : imports) SafeFiles.removeImport(file);
        webView.stopLoading();
        webView.destroy();
        PortableRuntime.detach(isFinishing() && !isChangingConfigurations());
        super.onDestroy();
    }
}
