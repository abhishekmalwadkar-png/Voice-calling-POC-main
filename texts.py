"""Voice prompts (TTS) per language, with template ids/versions for the audit trail.

Placeholders: {t} ticket, {s} site, {alarm} optional alarm sentence, {ack}/{rej} DTMF digits,
{name} technician name, {reason} escalation reason sentence, {n} number of calls.
"""
from xml.sax.saxutils import escape

from config import CFG

TEMPLATES = {  # purpose -> (template id, version) recorded on every call
    "technician": ("uc1_technician_ack", "v2"),
    "escalation": ("uc1_escalation", "v3"),
    "reminder": ("uc1_followup_reminder", "v1"),
}

LANGS = {
    "en": {
        "name": "English", "voice": "Polly.Aditi", "language": "en-IN",
        "menu": "For English, press {n}.",
        "alarm": " Alarm {a}.",
        "greet": "Hello.", "greet_name": "Hello {rname}.",
        "technician": "{greet} This is the Automation Edge helpdesk. Ticket {t} has been assigned to you, "
                      "at site {s}.{alarm} Press {ack} to acknowledge this ticket. Press {rej} to reject it.",
        "escalation": "{greet} This is the Automation Edge helpdesk escalation. Ticket {t}, at site {s}.{alarm} "
                      "The technician {name} {reason}.",
        "escalation_question": "Should we assign this alert to another technician? "
                               "Press {ack} for yes. Press {rej} for no.",
        "reassign_yes": "Thank you. This alert will be assigned to another technician. Goodbye.",
        "reassign_no": "Thank you. This alert will not be assigned to another technician. Goodbye.",
        "reason_rejected": "has rejected the assignment",
        "reason_no_ack": "has not acknowledged the assignment after {n} calls",
        "emergency_technician": "{greet} This is an emergency alert. Ticket {t}, alert ID {alert_id}, "
                                "at {s}. Severity: {severity}. Issue: {desc}. "
                                "Please take action immediately.",
        "emergency_escalation": "{greet} This is the helpdesk emergency escalation for ticket {t}. Technician {name} {reason}. Please take action or ask technician to review it.",
        "emergency_reason_no_answer": "did not answer the call",
        "emergency_reason_rejected": "has rejected the assignment",
        "reminder": "Reminder from the Automation Edge helpdesk. Ticket {t}, at site {s}, is still waiting for "
                    "your status update. Please we request you to update departed, arrived or started in ServiceNow.",
        "ack": "Thank you. Your acknowledgement has been recorded. Goodbye.",
        "reject": "Your rejection has been recorded. The ticket will be escalated. Goodbye.",
        "invalid": "Sorry, that is not a valid option.",
        "noinput": "We did not receive any input. Goodbye.",
        "goodbye": "Goodbye.",
    },
    "ar": {
        "name": "Arabic", "voice": "Polly.Zeina", "language": "arb",
        "menu": "للعربية، اضغط {n}.",
        "alarm": " التنبيه {a}.",
        "greet": "مرحبا.", "greet_name": "مرحبا {rname}.",
        "technician": "{greet} هذا مكتب المساعدة من أوتوميشن إيدج. تم تعيين التذكرة {t} لك، في الموقع {s}.{alarm} "
                      "اضغط {ack} لتأكيد الاستلام. اضغط {rej} للرفض.",
        "escalation": "{greet} هذا تصعيد من مكتب المساعدة. التذكرة {t}، في الموقع {s}.{alarm} "
                      "الفني {name} {reason}.",
        "escalation_question": "هل نُسند هذا التنبيه إلى فني آخر؟ اضغط {ack} للموافقة. اضغط {rej} للرفض.",
        "reassign_yes": "شكرا لك. سيتم إسناد هذا التنبيه إلى فني آخر. مع السلامة.",
        "reassign_no": "شكرا لك. لن يتم إسناد هذا التنبيه إلى فني آخر. مع السلامة.",
        "reason_rejected": "رفض المهمة",
        "reason_no_ack": "لم يؤكد استلام المهمة بعد {n} مكالمات",
        "emergency_technician": "{greet} هذا تنبيه طوارئ. التذكرة {t}، برقم التنبيه {alert_id}. "
                                "الموقع {s}. مستوى الخطورة {severity}. المشكلة: {desc}. "
                                "يرجى اتخاذ الإجراء فورا.",
        "emergency_escalation": "{greet} هذا تصعيد طارئ للتذكرة {t}. الفني {name} {reason}. يرجى اتخاذ الإجراء أو مطالبة الفني بمراجعتها.",
        "emergency_reason_no_answer": "لم يرد على المكالمة",
        "emergency_reason_rejected": "رفض المهمة",
        "reminder": "تذكير من مكتب المساعدة. التذكرة {t} في الموقع {s} ما زالت بانتظار تحديث حالتك. "
                    "يرجى تحديث الحالة في نكست تاسك: غادرت، وصلت أو بدأت.",
        "ack": "شكرا لك. تم تسجيل تأكيد الاستلام. مع السلامة.",
        "reject": "تم تسجيل الرفض وسيتم تصعيد التذكرة. مع السلامة.",
        "invalid": "عذرا، هذا خيار غير صالح.",
        "noinput": "لم نتلق أي إدخال. مع السلامة.",
        "goodbye": "مع السلامة.",
    },
    # Twilio <Say> has no Urdu voice: spoken Urdu is written in Devanagari and read by the Hindi voice.
    "ur": {
        "name": "Urdu", "voice": "Polly.Aditi", "language": "hi-IN",
        "menu": "उर्दू के लिए {n} दबाइए।",
        "alarm": " अलार्म {a}।",
        "greet": "अस्सलाम-ओ-अलैकुम।", "greet_name": "अस्सलाम-ओ-अलैकुम {rname}।",
        "technician": "{greet} यह ऑटोमेशन एज हेल्पडेस्क है। टिकट {t} आपको दिया गया है, साइट {s}।{alarm} "
                      "टिकट क़ुबूल करने के लिए {ack} दबाइए। मुस्तरद करने के लिए {rej} दबाइए।",
        "escalation": "{greet} यह हेल्पडेस्क एस्केलेशन है। टिकट {t}, साइट {s}।{alarm} "
                       "टेक्नीशियन {name} {reason}।",
        "escalation_question": "क्या यह अलर्ट किसी दूसरे टेक्नीशियन को दे दिया जाए? "
                                "हाँ के लिए {ack} दबाइए। नहीं के लिए {rej} दबाइए।",
        "reassign_yes": "शुक्रिया। यह अलर्ट दूसरे टेक्नीशियन को दे दिया जाएगा। ख़ुदा हाफ़िज़।",
        "reassign_no": "शुक्रिया। यह अलर्ट दूसरे टेक्नीशियन को नहीं दिया जाएगा। ख़ुदा हाफ़िज़।",
        "reason_rejected": "ने असाइनमेंट मुस्तरद कर दिया है",
        "reason_no_ack": "ने {n} कॉल के बाद भी असाइनमेंट क़ुबूल नहीं किया",
        "emergency_technician": "{greet} यह एक इमरजेंसी अलर्ट है। टिकट {t}, अलर्ट आईडी {alert_id}। "
                                "साइट {s}। गंभीरता {severity}। मसला: {desc}। "
                                "बराए मेहरबानी फ़ौरन कार्रवाई कीजिए।",
        "emergency_escalation": "{greet} यह टिकट {t} का इमरजेंसी एस्केलेशन है। टेक्नीशियन {name} {reason}। बराए मेहरबानी कार्रवाई कीजिए या टेक्नीशियन से रिव्यू करने को कहिए।",
        "emergency_reason_no_answer": "ने कॉल का जवाब नहीं दिया",
        "emergency_reason_rejected": "ने असाइनमेंट मुस्तरद कर दिया है",
        "reminder": "हेल्पडेस्क की याद-दहानी। टिकट {t}, साइट {s}, पर आपका स्टेटस अपडेट अभी बाक़ी है। "
                    "बराए मेहरबानी नेक्स्ट टास्क में रवाना, पहुँच गए या काम शुरू, अपडेट कीजिए।",
        "ack": "शुक्रिया। आपकी तस्दीक़ दर्ज कर ली गई है। ख़ुदा हाफ़िज़।",
        "reject": "आपका इनकार दर्ज कर लिया गया है। टिकट आगे भेजा जाएगा। ख़ुदा हाफ़िज़।",
        "invalid": "माज़रत, यह दुरुस्त इंतिख़ाब नहीं है।",
        "noinput": "हमें कोई जवाब नहीं मिला। ख़ुदा हाफ़िज़।",
        "goodbye": "ख़ुदा हाफ़िज़।",
    },
    "hi": {
        "name": "Hindi", "voice": "Polly.Aditi", "language": "hi-IN",
        "menu": "हिंदी के लिए {n} दबाएं।",
        "alarm": " अलार्म {a}।",
        "greet": "नमस्ते।", "greet_name": "नमस्ते {rname}।",
        "technician": "{greet} यह ऑटोमेशन एज हेल्पडेस्क है। टिकट {t} आपको सौंपा गया है, साइट {s}।{alarm} "
                      "टिकट स्वीकार करने के लिए {ack} दबाएं। अस्वीकार करने के लिए {rej} दबाएं।",
        "escalation": "{greet} यह हेल्पडेस्क एस्केलेशन है। टिकट {t}, साइट {s}।{alarm} "
                      "तकनीशियन {name} {reason}।",
        "escalation_question": "क्या यह अलर्ट किसी दूसरे तकनीशियन को सौंप दिया जाए? "
                               "हाँ के लिए {ack} दबाएं। नहीं के लिए {rej} दबाएं।",
        "reassign_yes": "धन्यवाद। यह अलर्ट दूसरे तकनीशियन को सौंप दिया जाएगा। अलविदा।",
        "reassign_no": "धन्यवाद। यह अलर्ट दूसरे तकनीशियन को नहीं सौंपा जाएगा। अलविदा।",
        "reason_rejected": "ने असाइनमेंट अस्वीकार कर दिया है",
        "reason_no_ack": "ने {n} कॉल के बाद भी असाइनमेंट स्वीकार नहीं किया",
        "emergency_technician": "{greet} यह एक इमरजेंसी अलर्ट है। टिकट {t}, अलर्ट आईडी {alert_id}। "
                                "साइट {s}। गंभीरता {severity}। समस्या: {desc}। "
                                "कृपया तुरंत कार्रवाई करें।",
        "emergency_escalation": "{greet} यह टिकट {t} का इमरजेंसी एस्केलेशन है। तकनीशियन {name} {reason}। कृपया कार्रवाई करें या तकनीशियन से समीक्षा करने को कहें।",
        "emergency_reason_no_answer": "ने कॉल का जवाब नहीं दिया",
        "emergency_reason_rejected": "ने असाइनमेंट अस्वीकार कर दिया है",
        "reminder": "हेल्पडेस्क रिमाइंडर। टिकट {t}, साइट {s}, पर आपका स्टेटस अपडेट बाकी है। "
                    "कृपया नेक्स्ट टास्क में डिपार्टेड, अराइव्ड या स्टार्टेड अपडेट करें।",
        "ack": "धन्यवाद। आपकी स्वीकृति दर्ज कर ली गई है। अलविदा।",
        "reject": "आपका अस्वीकार दर्ज कर लिया गया है। टिकट आगे भेजा जाएगा। अलविदा।",
        "invalid": "क्षमा करें, यह मान्य विकल्प नहीं है।",
        "noinput": "हमें कोई इनपुट नहीं मिला। अलविदा।",
        "goodbye": "अलविदा।",
    },
}

# Language menu order (key 1, 2, 3 ...), used only when a recipient's language is "ask"
MENU = [c for c in CFG["menu_languages"].replace(" ", "").split(",") if c in LANGS] or ["en"]


def resolve_language(preferred):
    """(language, source): NexTask preference > configured fallback; 'ask' -> caller menu."""
    pref = (preferred or "").strip().lower()
    if pref in LANGS:
        return pref, "nextask"
    if pref == "ask":
        return None, "menu"
    fallback = CFG["default_language"] if CFG["default_language"] in LANGS else "en"
    return fallback, "fallback"


def spell(text):
    """Read short codes (INC0012345, ALM-77) character by character; longer text as words."""
    text = str(text or "").strip()
    return " ".join(text) if text and " " not in text and len(text) <= 14 else text


def site_words(ticket):
    number, name = ticket.get("site_number"), ticket.get("site_name")
    if number and name:
        return f"{spell(number)}, {name}"
    return spell(number) if number else (name or "unknown")


def greeting(lang, name):
    """'Hello Ahmed.' when the recipient's name is known, else 'Hello.'"""
    L = LANGS[lang]
    return L["greet_name"].format(rname=name) if name else L["greet"]


def clean_desc(desc):
    if not desc:
        return "Emergency alert reported"
    raw = str(desc).replace("\\n", "\n")
    lines = [
        line.strip()
        for line in raw.split("\n")
        if line.strip() and not line.strip().lower().startswith(("site:", "severity:", "alert:", "sla:", "equipment:", "fault:"))
    ]
    cleaned = ". ".join(lines) if lines else raw.replace("\n", " ").strip()
    return cleaned.rstrip(".")[:180].strip()


def voice_text(lang, key, ticket=None, **kw):
    L = LANGS[lang]
    kw.setdefault("greet", L["greet"])
    if ticket is not None:
        kw.setdefault("t", spell(ticket.get("ticket_id") or ""))
        kw.setdefault("s", site_words(ticket))
        kw.setdefault("alarm", L["alarm"].format(a=spell(ticket["alarm_id"])) if ticket.get("alarm_id") else "")
        kw.setdefault("alert_id", spell(ticket.get("alarm_id") or ""))
        kw.setdefault("severity", ticket.get("severity") or "Critical")
        kw.setdefault("desc", clean_desc(ticket.get("description")))
    return L[key].format(**kw)


def say(lang, text):
    v = LANGS.get(lang, LANGS["en"])
    return f'<Say voice="{v["voice"]}" language="{v["language"]}">{escape(text)}</Say>'
