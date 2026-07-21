package nyc.durham.vira.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairPayloadTest {
    private val good = """{"v":1,"kind":"vira-pair",
        "url":"http://hub.example.ts.net:8377",
        "device_id":"cd_0123456789ab","token":"tok-abc"}"""

    @Test fun parsesTheHubPayload() {
        val p = PairPayload.parse(good)!!
        assertEquals("http://hub.example.ts.net:8377", p.url)
        assertEquals("cd_0123456789ab", p.deviceId)
        assertEquals("tok-abc", p.token)
    }

    @Test fun trailingSlashTrimmed() {
        val p = PairPayload.parse(
            good.replace(":8377", ":8377/"))!!
        assertEquals("http://hub.example.ts.net:8377", p.url)
    }

    @Test fun rejectsWrongKindMissingFieldsAndJunk() {
        assertNull(PairPayload.parse(null))
        assertNull(PairPayload.parse(""))
        assertNull(PairPayload.parse("not json at all"))
        assertNull(PairPayload.parse("""{"kind":"other","url":"http://x",
            "device_id":"d","token":"t"}"""))
        assertNull(PairPayload.parse("""{"kind":"vira-pair","url":"",
            "device_id":"d","token":"t"}"""))
        assertNull(PairPayload.parse("""{"kind":"vira-pair",
            "url":"ftp://nope","device_id":"d","token":"t"}"""))
    }
}

class MsgTest {
    @Test fun jsonRoundTrip() {
        val m = Msg("+12125550123", "see you at 6", 1770000000000L,
                    "sms", "in", "history")
        val back = Msg.fromJson(JSONObject(m.toJson().toString()))
        assertEquals(m, back)
    }

    @Test fun batchShape() {
        val batch = Msg.batchJson(listOf(
            Msg("a", "x", 1L, "sms", "in", "live"),
            Msg("b", "y", 2L, "whatsapp", "in", "live")))
        val arr = batch.getJSONArray("messages")
        assertEquals(2, arr.length())
        assertEquals("whatsapp", arr.getJSONObject(1).getString("channel"))
    }
}

class NotifRulesTest {
    @Test fun channelPerApp() {
        assertEquals("whatsapp", NotifRules.channelFor(NotifRules.PKG_WHATSAPP))
        assertEquals("rcs", NotifRules.channelFor(NotifRules.PKG_MESSAGES))
        assertEquals("notification", NotifRules.channelFor("com.example"))
    }

    @Test fun summariesAreSkipped() {
        assertTrue(NotifRules.isSummaryText("Chats", "3 new messages"))
        assertTrue(NotifRules.isSummaryText("WhatsApp",
                                            "Checking for new messages"))
        assertTrue(NotifRules.isSummaryText(null, "hello"))
        assertTrue(NotifRules.isSummaryText("Ann", ""))
        assertFalse(NotifRules.isSummaryText("Ann", "lunch tomorrow?"))
    }

    @Test fun smsEchoOnlyForMessagesAppWithProviderMatch() {
        val bodies = listOf("on my way", "ok")
        assertTrue(NotifRules.isSmsEcho(NotifRules.PKG_MESSAGES,
                                        "on my way", bodies))
        // RCS: body not in the SMS provider — capture it
        assertFalse(NotifRules.isSmsEcho(NotifRules.PKG_MESSAGES,
                                         "an rcs message", bodies))
        // WhatsApp never checks the provider
        assertFalse(NotifRules.isSmsEcho(NotifRules.PKG_WHATSAPP,
                                         "on my way", bodies))
        // no READ_SMS: notifications are the only door — capture
        assertFalse(NotifRules.isSmsEcho(NotifRules.PKG_MESSAGES,
                                         "on my way", null))
    }
}
