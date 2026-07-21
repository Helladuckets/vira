package nyc.durham.vira.companion

import java.io.File

/** A tiny durable JSONL queue: captured messages append here and leave
 *  only after the hub confirms the batch. Survives process death; plain
 *  java.io so it unit-tests without Android. Synchronization lives in
 *  UploadQueue (single writer/drainer per process). */
class QueueFile(private val file: File) {

    fun append(line: String) {
        file.parentFile?.mkdirs()
        file.appendText(line.replace("\n", " ") + "\n")
    }

    fun peek(max: Int): List<String> {
        if (!file.exists()) return emptyList()
        return file.useLines { seq ->
            seq.filter { it.isNotBlank() }.take(max).toList()
        }
    }

    /** Drop the first [n] lines (a confirmed batch). */
    fun drop(n: Int) {
        if (!file.exists() || n <= 0) return
        val rest = file.readLines().filter { it.isNotBlank() }.drop(n)
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(if (rest.isEmpty()) "" else rest.joinToString("\n") + "\n")
        tmp.renameTo(file)
    }

    fun size(): Int {
        if (!file.exists()) return 0
        return file.useLines { seq -> seq.count { it.isNotBlank() } }
    }
}
