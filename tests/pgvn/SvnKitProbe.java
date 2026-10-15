import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import org.tmatesoft.svn.core.*;
import org.tmatesoft.svn.core.io.*;
import org.tmatesoft.svn.core.wc.SVNWCUtil;
import org.tmatesoft.svn.core.internal.io.svn.SVNRepositoryFactoryImpl;
import org.tmatesoft.svn.core.internal.io.dav.DAVRepositoryFactory;

/** Real SVNKit protocol client. Password travels on stdin, never argv/logs. */
public final class SvnKitProbe {
    private static SVNRepository open(String url, String user, char[] password, String config) throws SVNException {
        SVNRepository repository = SVNRepositoryFactory.create(SVNURL.parseURIEncoded(url), ISVNSession.KEEP_ALIVE);
        repository.setAuthenticationManager(SVNWCUtil.createDefaultAuthenticationManager(new File(config), user, password, false));
        return repository;
    }

    private static boolean denied(SVNException error) {
        SVNErrorMessage message = error.getErrorMessage();
        while (message != null) {
            int code = message.getErrorCode().getCode();
            if (code == 170001 || code == 215004 || code == 220004) return true;
            message = message.getChildErrorMessage();
        }
        return false;
    }

    public static void main(String[] args) throws Exception {
        SVNRepositoryFactoryImpl.setup();
        DAVRepositoryFactory.setup();
        BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        char[] password = input.readLine().toCharArray();
        SVNRepository a = open(args[1], args[3], password, args[4]);
        SVNRepository b = open(args[2], args[3], password, args[4]);
        try {
            if (args[0].equals("expect-database-failure")) {
                try {
                    a.getLatestRevision();
                    throw new AssertionError("Expected the unavailable database to return a known SVN error");
                } catch (SVNException error) {
                    SVNErrorMessage message = error.getErrorMessage();
                    while (message != null && message.getErrorCode() != SVNErrorCode.FS_GENERAL) message = message.getChildErrorMessage();
                    if (message == null) throw error;
                    System.out.println("{\"code\":" + message.getErrorCode().getCode() + "}");
                }
                return;
            }
            long before = a.getLatestRevision();
            if (args[0].equals("roundtrip")) {
                if (!a.getRepositoryUUID(true).equals(b.getRepositoryUUID(true))) throw new AssertionError("UUID mismatch");
                ByteArrayOutputStream content = new ByteArrayOutputStream();
                a.getFile("trunk/payload.bin", before, new SVNProperties(), content);
                if (!Arrays.equals(content.toByteArray(), Files.readAllBytes(Path.of(args[5])))) throw new AssertionError("Binary mismatch");
                ISVNEditor editor = a.getCommitEditor("SVNKit commit", null);
                editor.openRoot(before);
                editor.addDir(args.length > 6 ? args[6] : "java-svnkit", null, -1);
                editor.closeDir();
                editor.closeDir();
                long revision = editor.closeEdit().getNewRevision();
                if (revision != before + 1 || b.getLatestRevision() != revision) throw new AssertionError("Revision mismatch");
                a.setRevisionPropertyValue(revision, "pgvn:svnkit", SVNPropertyValue.create("java-value"));
                if (!"java-value".equals(SVNPropertyValue.getPropertyAsString(b.getRevisionPropertyValue(revision, "pgvn:svnkit")))) throw new AssertionError("Revprop mismatch");
                System.out.println("{\"roundtrip\":true,\"revision\":" + revision + "}");
            } else {
                ISVNEditor editor = args[0].equals("hold-commit") ? a.getCommitEditor("revoked commit", null) : null;
                System.out.println("READY");
                System.out.flush();
                if (input.readLine() == null) throw new AssertionError("Controller input closed");
                try {
                    if (editor == null) a.getLatestRevision();
                    else {
                        editor.openRoot(before);
                        editor.addDir("must-not-publish", null, -1);
                        editor.closeDir();
                        editor.closeDir();
                        editor.closeEdit();
                    }
                    throw new AssertionError("Stale session was accepted");
                } catch (SVNException error) {
                    if (!denied(error)) throw error;
                    System.out.println("{\"denied\":true}");
                }
            }
        } finally {
            a.closeSession();
            b.closeSession();
            Arrays.fill(password, '\0');
        }
    }
}
