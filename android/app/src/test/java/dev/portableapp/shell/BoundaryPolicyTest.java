package dev.portableapp.shell;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.Arrays;

/** Runs with javac/java without an Android SDK, and from the Gradle check task. */
public final class BoundaryPolicyTest {
    private static void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
    }

    public static void main(String[] args) throws Exception {
        BoundaryPolicy policy = new BoundaryPolicy("http://127.0.0.1:34123",
                Arrays.asList("/api/backup", "/api/export/outstanding.csv", "/api/local-backups/"));
        check(policy.isInternal("http://127.0.0.1:34123/assets/app.js"), "same origin resource rejected");
        for (String url : Arrays.asList("http://localhost:34123/", "http://127.0.0.1:34124/",
                "https://127.0.0.1:34123/", "http://127.0.0.1:34123@evil.example/",
                "http://evil.example/", "file:///data/private", "content://private/item",
                "http://127.0.0.1:34123/%2e%2e/private", "http://127.0.0.1:34123/a/../private",
                "http://127.0.0.1:34123//evil.example", "http://127.0.0.1:34123/%5cprivate")) {
            check(!policy.isInternal(url), "untrusted resource accepted: " + url);
        }
        check(policy.isDownload("http://127.0.0.1:34123/api/backup"), "backup rejected");
        check(policy.isDownload("http://127.0.0.1:34123/api/export/outstanding.csv?as_of=2026-10-12"), "report query rejected");
        check(policy.isDownload("http://127.0.0.1:34123/api/local-backups/snapshot.json"), "snapshot rejected");
        check(!policy.isDownload("http://127.0.0.1:34123/api/entries"), "unlisted API exported");
        check(!policy.isDownload("http://127.0.0.1:34123/api/local-backups/a/b"), "nested export accepted");
        check(!policy.isDownload("http://127.0.0.1:34123/api/local-backups/a%2fb"), "encoded path exported");
        check(!policy.isDownload("http://127.0.0.1:34123/api/backup#secret"), "fragment accepted");
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        BoundaryPolicy.copyBounded(new ByteArrayInputStream(new byte[] {1, 2, 3}), output, 3);
        check(Arrays.equals(output.toByteArray(), new byte[] {1, 2, 3}), "file bytes changed");
        try {
            BoundaryPolicy.copyBounded(new ByteArrayInputStream(new byte[] {1, 2, 3, 4}), new ByteArrayOutputStream(), 3);
            throw new AssertionError("oversized input accepted");
        } catch (BoundaryPolicy.SizeLimitException expected) { }
        try {
            BoundaryPolicy.copyBounded(new ByteArrayInputStream(new byte[] {1}), new ByteArrayOutputStream(), 0);
            throw new AssertionError("zero bound accepted a byte");
        } catch (BoundaryPolicy.SizeLimitException expected) { }
        check("ledger-backup.json".equals(BoundaryPolicy.safeFilename("../../ledger-backup.json", "backup.json")), "filename path not removed");
        check("backup.json".equals(BoundaryPolicy.safeFilename("..", "backup.json")), "invalid filename accepted");
        System.out.println("BoundaryPolicy: origin, export allowlist, traversal and bounded file copy passed");
    }
}
