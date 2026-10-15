import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.util.Arrays;
import org.tmatesoft.svn.core.*;
import org.tmatesoft.svn.core.io.*;
import org.tmatesoft.svn.core.io.diff.SVNDeltaGenerator;
import org.tmatesoft.svn.core.internal.io.dav.DAVRepositoryFactory;
import org.tmatesoft.svn.core.internal.io.svn.SVNRepositoryFactoryImpl;
import org.tmatesoft.svn.core.wc.SVNWCUtil;

/** Use the exact SVNKit bundle shipped with the installed Polarion. */
public class SVNKitProbe {
    public static void main(String[] args) throws Exception {
        DAVRepositoryFactory.setup();
        SVNRepositoryFactoryImpl.setup();
        SVNRepository repository = SVNRepositoryFactory.create(SVNURL.parseURIEncoded(args[0]));
        repository.setAuthenticationManager(SVNWCUtil.createDefaultAuthenticationManager(
                new File(args[3]), args[1], args[2].toCharArray(), false));
        try {
            long base = repository.getLatestRevision();
            byte[] content = new byte[1048576 + 257];
            for (int i = 0; i < content.length; i++) content[i] = (byte) (i % 256);
            byte[] property = new byte[] {'a', 0, 'b', (byte)255};
            ISVNEditor editor = repository.getCommitEditor("SVNKit binary commit", null);
            editor.openRoot(base);
            editor.addDir("svnkit", null, -1);
            editor.addFile("svnkit/file.bin", null, -1);
            editor.changeFileProperty("svnkit/file.bin", "test:binary", SVNPropertyValue.create("test:binary", property));
            editor.applyTextDelta("svnkit/file.bin", null);
            String checksum = new SVNDeltaGenerator().sendDelta("svnkit/file.bin", new ByteArrayInputStream(content), editor, true);
            editor.closeFile("svnkit/file.bin", checksum);
            editor.closeDir();
            editor.closeDir();
            SVNCommitInfo commit = editor.closeEdit();
            if (commit.getNewRevision() != base + 1) throw new AssertionError("Wrong SVNKit committed revision");
            SVNProperties props = new SVNProperties();
            ByteArrayOutputStream actual = new ByteArrayOutputStream();
            repository.getFile("svnkit/file.bin", -1, props, actual);
            if (!Arrays.equals(content, actual.toByteArray())) throw new AssertionError("SVNKit fulltext mismatch");
            if (!Arrays.equals(property, props.getBinaryValue("test:binary"))) throw new AssertionError("SVNKit property mismatch");
            System.out.println("SVNKit revision=" + commit.getNewRevision() + " bytes=" + actual.size());
        } finally {
            repository.closeSession();
        }
    }
}
