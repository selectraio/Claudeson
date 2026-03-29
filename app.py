import streamlit as st
import zipfile
import os
import json
import re
import fitz
import anthropic
import hashlib
import shutil
from pathlib import Path
from collections import defaultdict

st.set_page_config(page_title="TUS Eşleştirici", page_icon="🏥")

def extract_zip(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest)
    return dest

def pdf_to_text(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        return "\n".join(page.get_text() for page in doc)
    except:
        return ""

def question_fingerprint(text):
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"[^\w\s]", "", normalized)
    return hashlib.md5(normalized[:200].encode()).hexdigest()

st.title("🏥 TUS Soru-Slayt Eşleştirici")

# API Key
api_key = st.text_input("Claude API Key (sk-ant-...)", type="password")

# Dosya yükleme
col1, col2 = st.columns(2)
with col1:
    lectures_zip = st.file_uploader("📚 Ders Slaytları (ZIP)", type=["zip"])
with col2:
    exams_zip = st.file_uploader("📝 Çıkmış Sorular (ZIP)", type=["zip"])

if st.button("🚀 Başlat", type="primary"):
    if not api_key:
        st.error("API Key girin!")
    elif not lectures_zip or not exams_zip:
        st.error("Her iki ZIP'i de yükleyin!")
    else:
        progress_bar = st.progress(0)
        status = st.empty()
        
        try:
            work_dir = "/tmp/tus_app"
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir)
            os.makedirs(work_dir)
            
            # 1. Soruları çıkar
            status.text("📄 Sınav soruları okunuyor...")
            exam_dir = os.path.join(work_dir, "exams")
            with open(os.path.join(work_dir, "exams.zip"), "wb") as f:
                f.write(exams_zip.getvalue())
            extract_zip(os.path.join(work_dir, "exams.zip"), exam_dir)
            
            questions = []
            seen = set()
            for pdf_file in Path(exam_dir).rglob("*.pdf"):
                year = re.search(r"20\d{2}", pdf_file.name)
                year = year.group() if year else "bilinmiyor"
                text = pdf_to_text(str(pdf_file))
                raw_qs = re.split(r"\n(?=\d{1,3}[\.\)][\s\u00a0])", text)
                for q in raw_qs:
                    q = q.strip()
                    if len(q) < 30:
                        continue
                    fp = question_fingerprint(q)
                    if fp not in seen:
                        seen.add(fp)
                        questions.append({"text": q, "year": year, "source_file": pdf_file.name})
            
            progress_bar.progress(30)
            status.text(f"📚 {len(questions)} soru bulundu. Slaytlar okunuyor...")
            
            # 2. Slaytları oku
            lecture_dir = os.path.join(work_dir, "lectures")
            with open(os.path.join(work_dir, "lectures.zip"), "wb") as f:
                f.write(lectures_zip.getvalue())
            extract_zip(os.path.join(work_dir, "lectures.zip"), lecture_dir)
            
            subjects = {}
            for folder in Path(lecture_dir).iterdir():
                if folder.is_dir():
                    slides = []
                    for pdf in folder.rglob("*.pdf"):
                        text = pdf_to_text(str(pdf))
                        if text:
                            slides.append({"file": pdf.name, "text": text[:1000]})
                    if slides:
                        subjects[folder.name] = slides
            
            if not subjects:
                st.error("Ders klasörü bulunamadı!")
            else:
                progress_bar.progress(60)
                status.text(f"🤖 Claude ile {len(questions)} soru analiz ediliyor...")
                
                # 3. Claude ile sınıflandır
                client = anthropic.Anthropic(api_key=api_key)
                classified = defaultdict(list)
                
                batch_size = 5
                for i in range(0, min(len(questions), 50), batch_size):  # Max 50 soru (limit)
                    batch = questions[i:i+batch_size]
                    numbered = "\n".join(f"[{j+1}] {q['text'][:400]}" for j, q in enumerate(batch))
                    
                    prompt = f"""Bu TUS sorularını şu derslere ayır: {', '.join(subjects.keys())}
JSON formatında döndür: [{{"idx": 1, "ders": "DersAdı"}}]

Sorular:
{numbered}"""
                    
                    try:
                        resp = client.messages.create(
                            model="claude-3-haiku-20240307",
                            max_tokens=500,
                            messages=[{"role": "user", "content": prompt}]
                        )
                        raw = resp.content[0].text
                        raw = re.sub(r"```[a-z]*", "", raw).strip()
                        assignments = json.loads(raw)
                        for a in assignments:
                            idx = a["idx"] - 1
                            ders = a["ders"]
                            if 0 <= idx < len(batch) and ders in subjects:
                                classified[ders].append(batch[idx])
                    except Exception as e:
                        for q in batch:
                            classified["Diğer"].append(q)
                    
                    progress_bar.progress(60 + (i/len(questions))*30)
                
                # 4. Sonuçları göster
                progress_bar.progress(100)
                status.text("✅ Tamamlandı!")
                
                st.success(f"{len(questions)} soru işlendi, {len(classified)} ders bulundu.")
                
                for subject, qs in classified.items():
                    with st.expander(f"📚 {subject} ({len(qs)} soru)"):
                        for q in qs:
                            st.write(f"**{q['year']}:** {q['text'][:200]}...")
                
        except Exception as e:
            st.error(f"Hata: {str(e)}")
