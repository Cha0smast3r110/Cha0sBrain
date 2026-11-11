"""Audio-Tendenz: findet Gemma zu einer Beschreibung den daraus erzeugten Clip? (20 Clips, Zufall Top-1 = 5 %)"""
import json, time
from mmlib import embed_text, embed_media, cos

DE = {
 "typewriter": "einzelner Schreibmaschinenanschlag", "stamp": "Stempel knallt auf Papier",
 "shutter": "Kamera-Auslöser klickt", "whoosh": "schneller Wusch-Übergang",
 "riser": "spannungsgeladener ansteigender Ton", "static_burst": "kurzes Fernsehrauschen",
 "alarm_beep": "piepender Alarm", "keyboard_frenzy": "hektisches Tippen auf der Tastatur",
 "drone_hit": "tiefer bedrohlicher Bass-Schlag", "error_buzz": "Fehler-Summer aus der Quizshow",
 "notification_ping": "Benachrichtigungston Ping", "clock_tick": "tickende Uhr",
 "record_stop": "Plattenkratzer, Musik stoppt", "server_hum": "Brummen im Serverraum",
 "reverse_riser": "rückwärts gesaugter Wusch bis zur Stille", "tension_drone": "düsterer Spannungs-Klangteppich",
 "suspense_pulse": "pulsierender Synthesizer, spannend", "lofi_dark": "düsterer Lofi-Hip-Hop",
 "synthwave_tick": "Retro-Synthwave mit tickendem Arpeggio", "news_underscore": "neutrale Nachrichtenmusik im Hintergrund",
}
items = json.load(open("audio_items.json"))
t0 = time.monotonic(); A = {}; failed = []
for it in items:
    try:
        A[it["label"]] = embed_media(it["path"])
    except Exception as exc:
        failed.append((it["label"], str(exc)[:120]))
dt = (time.monotonic() - t0) / len(items)
res = {}
for name, qtext in (("deutsch", lambda it: DE[it["label"]]), ("englisch-prompt", lambda it: it["prompt"])):
    top1 = top3 = 0
    for it in items:
        if it["label"] not in A: continue
        q = embed_text(qtext(it))
        order = sorted(A, key=lambda k: -cos(q, A[k]))
        top1 += order[0] == it["label"]; top3 += it["label"] in order[:3]
    res[name] = (top1, top3, len(A))
    print(f"{name}: Top-1 {top1}/{len(A)}, Top-3 {top3}/{len(A)}")
print(f"{dt:.2f} s/Clip, fehlgeschlagen {len(failed)} {failed}")
json.dump({"res": res, "sec_per_clip": dt, "failed": failed}, open("audio_result.json", "w"))
