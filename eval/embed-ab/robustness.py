"""Vorab festgelegte Neben-Auswertungen zu score.py (S1b): echte vs. synthetische Fragen,
binäre Relevanz, Live-CLS vs. mean, Zweitlabeler-Übereinstimmung (Cohen kappa) auf
labels_s1b_x2.json. Gepaarter Bootstrap mit festem Seed wie score.py."""
import json, random, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); import score as s
D=str(HERE)+"/"
R={m:json.load(open(D+f"ranks_{m}.json")) for m in ["lfm","lfmcls","gemma"]}
def ev(lab, sel=lambda q:True, binar=False, a="lfmcls", b="gemma"):
    d=[];va=[];vb=[]
    for q,l in lab.items():
        if not sel(q): continue
        l={r:(1 if g>=1 else 0) if binar else int(g) for r,g in l.items()}
        if not any(g>=1 for g in l.values()): continue
        x=s.ndcg_at_k([y["ref"] for y in R[a][q]],l,5); y=s.ndcg_at_k([z["ref"] for z in R[b][q]],l,5)
        va.append(x); vb.append(y); d.append(y-x)
    rng=random.Random(42); bo=sorted(sum(rng.choice(d) for _ in d)/len(d) for _ in range(10000))
    return f"n={len(d)} {a}={sum(va)/len(va):.3f} {b}={sum(vb)/len(vb):.3f} Δ={sum(d)/len(d):+.3f} KI=[{bo[249]:+.3f},{bo[9749]:+.3f}] W:L={sum(x>1e-9 for x in d)}:{sum(x<-1e-9 for x in d)}"
lab=json.load(open(D+"labels.json"))
real=lambda q:q.startswith("r")
print("PRIMÄR alle       ", ev(lab))
print("nur echte Prompts ", ev(lab,real))
print("nur synthetisch   ", ev(lab,lambda q:not real(q)))
print("binär (grade>=1)  ", ev(lab,binar=True))
print("gemma vs lfm(mean)", ev(lab,a="lfm"))
print("lfmcls vs lfm     ", ev(lab,a="lfm",b="lfmcls"))
# Zweitlabeler
x2=json.load(open(D+"labels_s1b_x2.json")); agree=0; tot=0; cm={}
for q,l in x2.items():
    for r,g in l.items():
        g1=lab[q][r]; tot+=1; agree+=(g1==int(g)); cm[(g1,int(g))]=cm.get((g1,int(g)),0)+1
po=agree/tot; m1=[sum(v for (a,b),v in cm.items() if a==k)/tot for k in range(3)]; m2=[sum(v for (a,b),v in cm.items() if b==k)/tot for k in range(3)]
pe=sum(m1[k]*m2[k] for k in range(3)); print(f"Zweitlabeler: {tot} Labels, Übereinstimmung {po:.3f}, Cohen κ {(po-pe)/(1-pe):.3f}, Matrix {dict(sorted(cm.items()))}")
lab2={q:dict(l) for q,l in lab.items()}
for q,l in x2.items():
    for r,g in l.items(): lab2[q][r]=int(g)
qs=set(x2)
print("Stichprobe Erst  ", ev(lab,lambda q:q in qs)); print("Stichprobe Zweit ", ev(lab2,lambda q:q in qs))
