from __future__ import annotations
import re
from dataclasses import dataclass

ASCII_RE=re.compile(r'[A-Za-z0-9_\-\.]+')
CJK_RE=re.compile(r'[\u4e00-\u9fff]+')


def tokenize(text:str)->set[str]:
    text=(text or '').lower()
    out=set(x.lower() for x in ASCII_RE.findall(text) if len(x)>=2)
    for block in CJK_RE.findall(text):
        if len(block)==1: out.add(block)
        else:
            out.update(block[i:i+2] for i in range(len(block)-1))
            if len(block)<=6: out.add(block)
    return out


def jaccard(a:set[str],b:set[str])->float:
    return len(a&b)/len(a|b) if a or b else 0.0

@dataclass
class CaseSignature:
    case_id:str
    summary:str
    hypothesis_codes:set[str]
    fault_domains:set[str]
    version_tokens:set[str]

class CaseSimilarity:
    version='1.0.0'
    def score(self,a:CaseSignature,b:CaseSignature)->tuple[float,dict]:
        text=jaccard(tokenize(a.summary),tokenize(b.summary))
        hypotheses=jaccard(a.hypothesis_codes,b.hypothesis_codes)
        domains=jaccard(a.fault_domains,b.fault_domains)
        versions=jaccard(a.version_tokens,b.version_tokens)
        score=0.45*text+0.35*hypotheses+0.15*domains+0.05*versions
        return score,{'text_jaccard':round(text,4),'hypothesis_jaccard':round(hypotheses,4),'domain_jaccard':round(domains,4),'version_jaccard':round(versions,4),'algorithm_version':self.version}
