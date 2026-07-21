package nyc.durham.vira.companion

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class QueueFileTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun q() = QueueFile(File(tmp.root, "q.jsonl"))

    @Test fun appendPeekDropRoundTrip() {
        val q = q()
        q.append("""{"a":1}""")
        q.append("""{"a":2}""")
        q.append("""{"a":3}""")
        assertEquals(3, q.size())
        assertEquals(listOf("""{"a":1}""", """{"a":2}"""), q.peek(2))
        q.drop(2)
        assertEquals(listOf("""{"a":3}"""), q.peek(10))
        q.drop(1)
        assertEquals(0, q.size())
    }

    @Test fun newlinesInPayloadsCannotSplitRecords() {
        val q = q()
        q.append("line one\nline two")
        assertEquals(1, q.size())
        assertEquals("line one line two", q.peek(1)[0])
    }

    @Test fun emptyAndMissingFileAreCalm() {
        val q = q()
        assertEquals(0, q.size())
        assertEquals(emptyList<String>(), q.peek(5))
        q.drop(3)   // dropping from nothing is a no-op
        assertEquals(0, q.size())
    }
}
