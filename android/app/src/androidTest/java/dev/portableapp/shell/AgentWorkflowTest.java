package dev.portableapp.shell;

import static org.junit.Assert.*;

import android.content.Context;
import android.net.Uri;
import android.webkit.WebView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Business acceptance against the APK's real Python through authenticated WebView fetch. */
@RunWith(AndroidJUnit4.class)
public final class AgentWorkflowTest {
    private static String evaluate(ActivityScenario<MainActivity> scenario, String script) throws Exception {
        AtomicReference<String> result = new AtomicReference<>();
        CountDownLatch ready = new CountDownLatch(1);
        scenario.onActivity(activity -> ((WebView) activity.findViewById(MainActivity.WEB_VIEW_ID))
                .evaluateJavascript(script, value -> { result.set(value); ready.countDown(); }));
        assertTrue("WebView JavaScript timed out", ready.await(10, TimeUnit.SECONDS));
        return result.get();
    }

    private static void awaitPage(ActivityScenario<MainActivity> scenario) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(40);
        while (System.nanoTime() < deadline) {
            try {
                if ("true".equals(evaluate(scenario,
                        "document.readyState === 'complete' && document.querySelector('button') !== null"))) return;
            } catch (NullPointerException ignored) { }
            Thread.sleep(100);
        }
        fail("The packaged AgentLab dashboard did not render");
    }

    private static JSONObject fetch(ActivityScenario<MainActivity> scenario, String path, JSONObject body,
                                    int expectedStatus) throws Exception {
        JSONObject options = new JSONObject().put("method", body == null ? "GET" : "POST");
        if (body != null) {
            options.put("headers", new JSONObject().put("Content-Type", "application/json"));
            options.put("body", body.toString());
        }
        evaluate(scenario, "window.__agentWorkflowResult=null;"
                + "fetch(" + JSONObject.quote(path) + "," + options + ")"
                + ".then(async r=>{window.__agentWorkflowResult={status:r.status,"
                + "disposition:r.headers.get('content-disposition'),body:await r.json()}})"
                + ".catch(e=>{window.__agentWorkflowResult={status:-1,error:String(e)}});true");
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(40);
        while (System.nanoTime() < deadline) {
            Object value = new JSONTokener(evaluate(scenario,
                    "window.__agentWorkflowResult===null?'':JSON.stringify(window.__agentWorkflowResult)")).nextValue();
            if (value instanceof String && !((String) value).isEmpty()) {
                JSONObject reply = new JSONObject((String) value);
                assertEquals(reply.toString(), expectedStatus, reply.getInt("status"));
                return reply;
            }
            Thread.sleep(100);
        }
        throw new AssertionError("Packaged WebView request timed out: " + path);
    }

    private static JSONObject body(ActivityScenario<MainActivity> scenario, String path, JSONObject input,
                                   int status) throws Exception {
        return fetch(scenario, path, input, status).getJSONObject("body");
    }

    private static int count(JSONArray events, String type, String tool) throws Exception {
        int result = 0;
        for (int i = 0; i < events.length(); i++) {
            JSONObject event = events.getJSONObject(i);
            assertEquals("Trace order is not contiguous", i + 1, event.getInt("sequence"));
            if (type.equals(event.getString("event_type"))
                    && (tool == null || tool.equals(event.getJSONObject("payload").optString("tool_name")))) result++;
        }
        return result;
    }

    private static JSONArray trace(ActivityScenario<MainActivity> scenario, String id) throws Exception {
        return body(scenario, "/v1/runs/" + id + "/trace", null, 200).getJSONArray("events");
    }

    @Test public void packagedScenariosRetryApproveEvaluateAndExportRealEvidence() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session session = PortableRuntime.obtain(context);
            assertTrue(session.metadata.getString("slug").startsWith("agent-reliability-lab"));
            awaitPage(scenario);
            assertEquals("\"\"", evaluate(scenario, "document.cookie"));
            assertEquals(6, body(scenario, "/v1/scenarios", null, 200).getJSONArray("items").length());

            JSONObject retry = body(scenario, "/v1/runs", new JSONObject()
                    .put("scenario_id", "timeout-recovery").put("mode", "resilient"), 201);
            assertEquals("succeeded", retry.getString("status"));
            assertEquals(4, retry.getInt("attempt_count"));
            JSONArray retryEvents = trace(scenario, retry.getString("id"));
            assertEquals(4, count(retryEvents, "tool.attempt.started", null));
            assertEquals(1, count(retryEvents, "tool.attempt.failed", null));
            assertEquals(3, count(retryEvents, "tool.attempt.succeeded", null));

            JSONObject fragile = body(scenario, "/v1/runs", new JSONObject()
                    .put("scenario_id", "timeout-recovery").put("mode", "fragile"), 201);
            assertEquals("failed", fragile.getString("status"));
            assertEquals(2, fragile.getInt("attempt_count"));

            JSONObject waiting = body(scenario, "/v1/runs", new JSONObject()
                    .put("scenario_id", "approval-reconstruction").put("mode", "resilient"), 201);
            assertEquals("waiting_approval", waiting.getString("status"));
            JSONObject pending = waiting.getJSONObject("pending_approval");
            String approvalPath = "/v1/runs/" + waiting.getString("id") + "/approvals";
            JSONObject decision = new JSONObject().put("actor", "android-acceptance").put("allow", true)
                    .put("action_step", pending.getInt("action_step"))
                    .put("action_fingerprint", pending.getString("action_fingerprint"))
                    .put("reason", "Review the packaged synthetic rollback");
            JSONObject stale = new JSONObject(decision.toString()).put("action_fingerprint",
                    "0000000000000000000000000000000000000000000000000000000000000000");
            assertEquals("run_conflict", body(scenario, approvalPath, stale, 409)
                    .getJSONObject("error").getString("code"));
            assertEquals(0, count(trace(scenario, waiting.getString("id")), "approval.recorded", null));
            assertEquals(0, count(trace(scenario, waiting.getString("id")), "tool.attempt.started", "prepare_rollback"));
            scenario.recreate();
            awaitPage(scenario);
            assertEquals("waiting_approval", body(scenario, "/v1/runs/" + waiting.getString("id"),
                    null, 200).getString("status"));
            for (int i = 0; i < 2; i++) {
                assertEquals("succeeded", body(scenario, approvalPath, decision, 200).getString("status"));
            }
            JSONArray approvedEvents = trace(scenario, waiting.getString("id"));
            assertEquals(1, count(approvedEvents, "approval.recorded", null));
            assertEquals(1, count(approvedEvents, "tool.attempt.succeeded", "prepare_rollback"));

            JSONObject report = body(scenario, "/v1/evaluations",
                    new JSONObject().put("suite", "incident-response"), 201);
            JSONObject provenance = report.getJSONObject("provenance");
            assertEquals("mobile/v1", provenance.getString("report_version"));
            assertEquals("mobile-exact/v1", provenance.getString("grader_version"));
            assertEquals(session.metadata.getString("build_revision"), provenance.getString("git_revision"));
            Set<String> ids = new HashSet<>();
            for (String mode : new String[]{"fragile", "resilient"}) {
                JSONObject result = report.getJSONObject("modes").getJSONObject(mode);
                JSONObject metrics = result.getJSONObject("metrics");
                assertEquals(6, metrics.getInt("case_count"));
                assertEquals(mode.equals("resilient") ? 6 : 4, metrics.getInt("case_correct_count"));
                assertEquals(mode.equals("resilient") ? 2 : 0, metrics.getInt("recovered_transient_fault_count"));
                assertEquals(0, metrics.getInt("invalid_output_accepted_count"));
                JSONArray cases = result.getJSONArray("cases");
                assertEquals(6, cases.length());
                for (int i = 0; i < cases.length(); i++) {
                    JSONObject item = cases.getJSONObject(i);
                    String id = item.getString("run_id");
                    assertTrue("Evaluation reused a run", ids.add(id));
                    assertEquals(item.getInt("attempt_count"), count(trace(scenario, id),
                            "tool.attempt.started", null));
                }
            }
            assertEquals(12, ids.size());
            assertEquals(report.toString(), body(scenario, "/v1/evaluations/"
                    + report.getString("evaluation_id"), null, 200).toString());

            String exportPath = "/v1/export?run_id=" + retry.getString("id");
            JSONObject downloaded = fetch(scenario, exportPath, null, 200);
            assertEquals("attachment; filename=\"trace-" + retry.getString("id") + ".json\"",
                    downloaded.getString("disposition"));
            assertEquals(retry.getString("id"), downloaded.getJSONObject("body")
                    .getJSONObject("run").getString("id"));
            assertEquals(retryEvents.length(), downloaded.getJSONObject("body").getJSONArray("events").length());

            SafeFiles.StagedFile staged = SafeFiles.download(context, session, session.origin + exportPath);
            try {
                Context tests = InstrumentationRegistry.getInstrumentation().getContext();
                Uri chosen = Uri.parse("content://" + tests.getPackageName() + ".fixtures/saved.json");
                SafeFiles.saveDocument(context, staged, chosen);
                try (InputStream input = context.getContentResolver().openInputStream(chosen)) {
                    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                    BoundaryPolicy.copyBounded(input, bytes, 1024 * 1024);
                    JSONObject saved = new JSONObject(bytes.toString(StandardCharsets.UTF_8.name()));
                    assertEquals(retry.getString("id"), saved.getJSONObject("run").getString("id"));
                    assertEquals(1, count(saved.getJSONArray("events"), "tool.attempt.failed", null));
                }
            } finally { staged.file.delete(); }
        }
    }
}
