"""Map a job onto one role from a fixed, detailed list.

Every source uses its own labels (or none), so the role comes from the job
title first, then from the department and the source's own labels. Rules are
checked in order and the first match wins, so more specific roles come first
("Security Analyst" is Security, "Data Analyst" is Analyst, "Product Designer"
is Design, "Product Marketing Manager" is Marketing).

Titles in German, Spanish, Portuguese, French, Polish and Hungarian are
covered for the most common roles, because several watched companies post in
those languages.
"""
from __future__ import annotations

import re

CATEGORIES = [
    "Software Engineering",
    "DevOps & Cloud",
    "Data Engineering",
    "Data Science & Machine Learning",
    "Analyst",
    "Product Management",
    "Project & Program Management",
    "Consulting",
    "Design & Creative",
    "Cybersecurity",
    "IT & Systems",
    "Hardware & Engineering",
    "Science & Research",
    "Sales",
    "Account Management & Customer Success",
    "Marketing",
    "Communications & PR",
    "Writing & Content",
    "Customer Support",
    "Finance & Accounting",
    "HR & Recruiting",
    "Legal & Compliance",
    "Operations & Strategy",
    "Supply Chain & Logistics",
    "Skilled Trades & Manufacturing",
    "Healthcare",
    "Fitness & Wellness",
    "Education & Training",
    "Retail & Hospitality",
    "Administrative",
    "Facilities & Security Services",
    "Other",
]

# Order matters: the first matching rule wins.
_RULES: list[tuple[str, str]] = [
    ("Retail & Hospitality",
     r"style advisor|beauty advisor|sales associate|retail (associate|sales|team|store|assistant)|store (associate|manager|lead|"
     r"assistant|employee|team)|cashier|barista|bartender|\bserver\b(?!.*\b(engineer|engineering|developer|administrator|hardware|architect|admin)\b)(?<!virtual server)|waiter|waitress|crew member|housekeep|front desk|"
     r"concierge|dishwasher|line cook|prep cook|\bcook\b|\bchef\b|kitchen|restaurant|hotel|hostess|valet|"
     r"verkäufer|vendeur|vendeuse|conseill[eè]re? de vente|conseiller\.[eè]re de vente|camarero|atendente de loja|"
     r"filialleit|filiale|kassierer"),
    ("Finance & Accounting", r"financial (advisor|planner)|wealth (advisor|manager)|investment advisor"),
    ("Skilled Trades & Manufacturing",
     r"welder|welding|machinist|electrician|plumber|carpenter|forklift|\bcnc\b|fabricator|pipefitter|millwright|"
     r"production (coordinator|planner|scheduler|lead|supervisor|worker|associate|operator)|assembler"),
    ("Fitness & Wellness", r"studio manager|fitness manager|club manager"),
    # --- very specific titles that would otherwise be caught by broader rules
    ("Facilities & Security Services",
     r"security (officer|guard|agent|supervisor|patrol)|\bguard\b|pracownik ochrony|ochron|vigilante|sicherheitsmitarbeiter|"
     r"agent de s[ée]curit[ée]|ordningsvakt|väktare|vakt\b|bevakning|vagt\b|custodian|janitor|cleaner|cleaning|\bfacilit(y|ies)\b|building maintenance|groundskeep|porter\b"),
    ("Cybersecurity",
     r"cyber|infosec|information security|security (engineer|analyst|architect|researcher|specialist|operations|consultant|manager|lead)|"
     r"\bsoc\b|penetration|pentest|appsec|application security|cloud security|product security|threat|vulnerabilit|"
     r"trust (and|&) safety|\bfraud\b|\babuse\b|detection engineer|incident response|red team|identity (and|&) access|\biam\b|"
     r"kiberbiztons|ciberseguran|cybers[ée]curit|software engineer,? security"),
    ("Data Science & Machine Learning",
     r"data scien|machine learning|\bml\b|deep learning|computer vision|\bnlp\b|research scientist|applied scientist|"
     r"\bai\b (engineer|researcher|scientist)|(ai|ml) (infrastructure|platform|research)|llm|reinforcement learning|"
     r"quantitative research|\bquant\b|statistician|decision scien"),
    ("Data Engineering",
     r"data engineer|analytics engineer|\betl\b|data platform|data architect|data infrastructure|big data|data warehouse|"
     r"database (engineer|administrator|developer)|\bdba\b|data integration|data pipeline|engineer[^,]*, data\b"),
    ("Analyst",
     r"analyst|analista|analytiker|analityk|elemz[őo]|business intelligence|\bbi (developer|engineer|specialist)\b|"
     r"reporting specialist|insights (manager|lead|specialist)"),
    ("Design & Creative",
     r"designer|\bdesign (lead|manager|director|intern)|\bux\b|\bui\b|user research|ux research|creative director|art director|"
     r"illustrat|animator|motion graphic|graphic|video (editor|producer|production)|videograph|photograph|"
     r"content creator|3d artist|\bartist\b|game design|level design|diseñador|designer gráfico|grafik"),
    ("Project & Program Management",
     r"project manag|program manag|programme manag|project coordinat|program coordinat|project lead|project director|"
     r"\btpm\b|\bpmo\b|scrum master|agile coach|delivery manag|project engineer|project controls|project admin|"
     r"projektleit|projektmanag|projektkoordin|chef de projet|gerente de proyecto|jefe de proyecto|gerente de projeto|"
     r"projektmened|kierownik projektu"),
    ("Product Management",
     r"product manag|product owner|product lead|head of product|\bvp,? product|director,? product|product director|"
     r"chief product|group product|\bpm\b|product operations|product strategy|produktmanag"),
    ("Consulting",
     r"consult|advisory|\bberater|beratung|consultor|conseil"),
    ("Marketing",
     r"marketing|\bseo\b|\bsem\b|growth (manager|lead|marketer|specialist)|brand (manager|strateg|director|lead)|"
     r"demand gen|lifecycle|crm (manager|specialist)|social media|community (manager|lead)|influencer|"
     r"campaign|media buyer|paid (media|search|social)|go-to-market|\bgtm\b|mercadotecnia|mercadeo"),
    ("Communications & PR",
     r"communications|public relations|\bpr (manager|specialist|lead)|media relations|press|spokes|corporate affairs|"
     r"public affairs|kommunikation|comunicaci[oó]n|comunica[cç][aã]o"),
    ("Writing & Content",
     r"writer|editor|copywrit|content (strateg|manager|lead|specialist|producer|designer)|journalist|reporter|"
     r"translator|translation|localization|technical writ|proofread|redakt"),
    ("Account Management & Customer Success",
     r"account manag|customer success|client success|client (partner|manager|services)|relationship manag|"
     r"key account|onboarding (manager|specialist)|implementation (manager|consultant|specialist|lead)|renewals|"
     r"partner manag|customer experience manag|account director|client director|account lead"),
    ("Sales",
     r"sales|account executive|\bae\b|business development|\bbdr\b|\bsdr\b|partnerships|revenue|deal desk|"
     r"solutions? (engineer|architect|specialist|consultant|manager)|pre-?sales|account development|development representative|seller|selling|enablement|sales advisor|membership advisor|"
     r"inside sales|territory|channel (manager|partner)|"
     r"vertrieb|verkauf|ventas|vendas|vendedor|commercial|értékesít|sprzeda|handlowiec"),
    ("IT & Systems",
     r"\bit\b (support|specialist|technician|manager|administrator|engineer|analyst|project|operations|director|systems|service)|"
     r"system(s)? administrator|sysadmin|network (engineer|administrator|technician)|desktop support|"
     r"it-|informatik|informática|tecnologia da informa|\bsap\b|erp (consultant|specialist|developer)|salesforce (admin|developer)"),
    ("Customer Support",
     r"support (specialist|engineer|agent|representative|associate|analyst|lead|manager)|customer support|"
     r"technical support|customer service|customer care|help ?desk|call center|contact center|service desk|"
     r"customer experience|technical services|member services|provider services|kundenservice|atenci[oó]n al cliente|atendimento"),
    ("DevOps & Cloud",
     r"devops|dev ops|\bsre\b|site reliability|cloud (engineer|architect|operations|infrastructure)|platform engineer|"
     r"infrastructure engineer|kubernetes|release engineer|build engineer|production engineer|reliability engineer"),
    ("Software Engineering",
     r"software|developer|programmer|front[- ]?end|back[- ]?end|full[- ]?stack|\bweb\b|mobile engineer|\bios\b|android|"
     r"\bsde\b|\bswe\b|\bqa\b|quality assurance|test automation|sdet|test engineer|engineering manager|"
     r"member of technical staff|staff engineer|principal engineer|enterprise architect|software architect|"
     r"implementation architect|technical architect|application architect|firmware|embedded|game (programmer|developer)|"
     r"entwickler|desarrollador|desenvolvedor|développeur|programista|fejleszt[őo]|\bcoder\b"),
    ("Hardware & Engineering",
     r"mechanical|electrical|electronic|civil engineer|structural|manufacturing engineer|process engineer|quality engineer|"
     r"hardware|mechatron|aerospace|avionics|propulsion|\brf\b|chemical engineer|industrial engineer|systems engineer|"
     r"reliability|design engineer|test (and|&) evaluation|robotics|controls engineer|automation engineer|"
     r"materials engineer|thermal|optical|semiconductor|asic|fpga|pcb|validation engineer|"
     r"ingenieur|ingeniero|engenheir|ingénieur|inżynier|mérnök"),
    ("Science & Research",
     r"scientist|research (associate|assistant|fellow)|researcher|laborator|\blab\b|chemist|biolog|physicist|"
     r"clinical research|r&d|postdoc|phd|forschung|investigador|pesquisador"),
    ("Finance & Accounting",
     r"financ|accountant|accounting|\btax\b|payroll|controller|controlling|fp&a|treasury|audit|bookkeep|billing|"
     r"accounts (payable|receivable)|credit|actuar|investment|banking|underwrit|portfolio|equity research|"
     r"buchhalt|contab|comptab|księgow|könyvel|pénzügy|finanz"),
    ("HR & Recruiting",
     r"recruit|talent|people (ops|operations|partner|business partner|team)|\bhr\b|human resources|sourcer|"
     r"compensation|benefits|learning (and|&) development|\bl&d\b|workforce|personalreferent|recursos humanos|"
     r"ressources humaines|kadr|toborz|hr-"),
    ("Legal & Compliance",
     r"legal|counsel|lawyer|attorney|paralegal|compliance|regulatory|privacy|governance|public policy|policy (lead|manager|director)|\bgrc\b|contracts? manag|"
     r"jurist|abogado|advogado|juriste|prawnik|jogász"),
    ("Operations & Strategy",
     r"operations|bizops|strategy|strategist|chief of staff|ehs\b|hse\b|health (and|&) safety|safety (advisor|specialist|manager|officer|lead)|business analyst|corporate development|general manager|"
     r"operations manager|\bops\b|revenue operations|sales operations|program operations|betrieb|operaciones|opera[cç][oõ]es"),
    ("Supply Chain & Logistics",
     r"supply chain|logistic|procurement|purchasing|\bbuyer\b|sourcing (manager|specialist|lead)|inventory|warehouse|"
     r"fulfil|shipping|distribution|transportation|fleet|dispatch|planner|demand planning|import|export|"
     r"\bdriver\b|courier|vendor manag|supplier|delivery (driver|associate)|einkauf|lager|logística|compras|achats|zakup|logisztik|beszerz"),
    ("Skilled Trades & Manufacturing",
     r"technician|technik|técnico|techniker|welder|welding|machinist|mechanic|electrician|assembler|assembly|"
     r"\boperator\b|operador|operário|operario|operátor|production (worker|associate|operator|technician|supervisor)|"
     r"maintenance|cnc|fabricat|plumber|hvac|forklift|construction|installer|inspector|carpenter|painter|"
     r"monteur|mechatroniker|elektriker|karbantart|wózk|craftsman|tradesperson|\bshift\b"),
    ("Healthcare",
     r"nurse|nursing|\brn\b|\blpn\b|physician|doctor|medical|clinical|clinic|pharmac|dental|dentist|hygienist|"
     r"caregiver|patient|health care|healthcare|therapist|psycholog|counselor|social worker|phlebotom|radiolog|"
     r"surgeon|paramedic|\bemt\b|optometr|veterinar|pflege|enfermer|enfermagem|krankenpfleg"),
    ("Fitness & Wellness",
     r"personal trainer|fitness|pilates|yoga|barre|spin instructor|group fitness|massage|esthetician|spa\b|"
     r"wellness|nutrition|dietitian|\bcoach\b|coach (fitness|tier)|tier [0-9] coach|lifeguard|swim instructor"),
    ("Education & Training",
     r"teacher|tutor|instructor|professor|lecturer|curriculum|educat|teaching|trainer|training (specialist|manager|coordinator)|"
     r"instructional|learning designer|learning management|e-learning|digital learning|school|academic|lehrer|profesor|professeur|nauczyciel|tanár"),
    ("Retail & Hospitality",
     r"retail|store|sales associate|cashier|barista|waiter|waitress|bartender|\bcook\b|chef|kitchen|"
     r"restaurant|food|hospitality|hotel|housekeep|front desk|concierge|host\b|hostess|crew member|team member|"
     r"stylist|style advisor|merchandis|\bshop\b|boutique|guest|valet|dishwasher|catering|verkäufer|vendeur|camarero|"
     r"atendente|recepcion"),
    ("Administrative",
     r"administrative|admin assistant|executive assistant|\bea\b|office manager|office coordinator|receptionist|"
     r"secretary|clerk|data entry|assistant|assistent|asistent|coordinator|sachbearbeit|administrat|asystent"),
]
_COMPILED = [(cat, re.compile(pat, re.I)) for cat, pat in _RULES]

# Source and department labels that map straight onto a role.
_LABEL_MAP = [
    ("Software Engineering", r"engineering|developer|software|it & development|technology"),
    ("Data Science & Machine Learning", r"data science|machine learning|\bai\b|research"),
    ("Analyst", r"analytics|business intelligence"),
    ("Design & Creative", r"design|creative"),
    ("Product Management", r"product"),
    ("Sales", r"sales|business development"),
    ("Marketing", r"marketing"),
    ("Customer Support", r"support|customer service"),
    ("Finance & Accounting", r"finance|accounting"),
    ("HR & Recruiting", r"people|human resources|\bhr\b|recruit"),
    ("Legal & Compliance", r"legal|compliance"),
    ("Operations & Strategy", r"operations|strategy"),
    ("Supply Chain & Logistics", r"logistics|supply chain|procurement|purchasing"),
    ("Healthcare", r"health|medical|clinical"),
    ("Education & Training", r"education|teaching"),
    ("Writing & Content", r"writing|content|editorial"),
    ("Retail & Hospitality", r"retail|hospitality|restaurant"),
    ("Skilled Trades & Manufacturing", r"manufacturing|production|maintenance|trades"),
    ("Administrative", r"administrative|admin"),
]
_LABEL_COMPILED = [(cat, re.compile(pat, re.I)) for cat, pat in _LABEL_MAP]


_GENERIC_ENGINEER = re.compile(r"engineer|engineering|ingenieur|ingeniero|engenheir|ingénieur|inżynier|mérnök", re.I)
_HARDWARE_HINT = re.compile(r"hardware|mechanical|electrical|manufactur|production|vehicle|aerospace|launch|"
                            r"propulsion|avionics|starship|starlink|falcon|dragon|build|facilities|plant|factory", re.I)


def categorize(title: str, hints: list[str] | None = None) -> str:
    title = title or ""
    for cat, rx in _COMPILED:
        if rx.search(title):
            return cat
    hint_text = " ".join(h for h in (hints or []) if h)
    # A plain "Engineer" title: hardware if the team says so, otherwise software.
    if _GENERIC_ENGINEER.search(title):
        return "Hardware & Engineering" if _HARDWARE_HINT.search(hint_text + " " + title) else "Software Engineering"
    for cat, rx in _LABEL_COMPILED:
        if rx.search(hint_text):
            return cat
    for cat, rx in _COMPILED:
        if rx.search(hint_text):
            return cat
    return "Other"
