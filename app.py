import streamlit as st
import zipfile
import os
import json
import re
import fitz
import anthropic
import hashlib
import shutil
import io
from pathlib import Path
from collections import defaultdict

st.set_page_config(page_title="TUS Eşleştirici", page_icon="🏥")

def extract_zip(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    if hasattr(zip_path, 'getvalue'):
        with open(os.path.join(dest, "temp.zip"), "wb") as f:
            f.write(zip_path.getvalue())
        zip_path = os.path.join(dest, "temp.zip")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest)
    return dest

def pdf_to_text(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        return "\n".join(page.get_text() for page in doc)
    except:
        return ""

def get_pdf_page_image(pdf_path, page_num):
    """PDF sayfasını görüntü olarak al"""
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_num]
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    except:
        return None

def question_fingerprint(text):
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"[^\w\s]", "", normalized)
    return hashlib.md5(normalized[:200].encode()).hexdigest()

def find_best_slide(question_text, slides, api_key):
    """Claude'a sorarak en uygun slaytı bul"""
    if not slides or not api_key:
        return None
    
    client = anthropic.Anthropic(api_key=api_key)
    candidates = slides[:10]  # İlk 10 slaytı kontrol et (token limiti için)
    
    numbered = "\n".join(f"[{i}] {s['text'][:300]}" for i, s in enumerate(candidates))
    
    prompt = f"""Bu soruyu çözmek için hangi slayt en uygun? Sadece slayt numarası (0-9) döndür.
    
Soru: {question_text[:500]}

Slaytlar:
{numbered}

Cevap (sadece sayı):"""

    try:
        resp = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}]
        )
        result = resp.content[0].text.strip()
        num = int(re.search(r'\d+', result).group())
        if 0 <= num < len(candidates):
            return candidates[num]
    except:
        pass
    return candidates[0] if candidates else None

st.title("🏥 TUS Soru-Slayt Eşleştirici")
st.write("Çıkmış soruları ders slaytlarıyla otomatik eşleştirir")

# API Key
api_key = st.text_input("Claude API Key (sk-ant-api03-...)", type="password")

col1, col2 = st.columns(2)
with col1:
    lectures_zip = st.file_uploader("📚 Ders Slaytları ZIP", type=["zip"])
with col2:
    exams_zip = st.file_uploader("📝 Çıkmış Sorular ZIP", type=["zip"])

if st.button("🚀 Başlat", type="primary", disabled=not (api_key and lectures_zip and exams_zip)):
    if not api_key.startswith("sk-"):
        st.error("Geçerli bir Claude API Key girin (sk- ile başlamalı)")
    else:
        progress = st.progress(0)
        status = st.empty()
        
        try:
            work_dir = "/tmp/tus_work"
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir)
            os.makedirs(work_dir)
            
            # 1. Sınav sorularını çıkar
            status.text("📄 Sınav soruları okunuyor...")
            exam_dir = os.path.join(work_dir, "exams")
            extract_zip(exams_zip, exam_dir)
            
            questions = []
            seen = set()
            
            for pdf_file in Path(exam_dir).rglob("*.pdf"):
                year = re.search(r"20\d{2}", pdf_file.name)
                year = year.group() if year else "bilinmiyor"
                text = pdf_to_text(str(pdf_file))
                
                # Soruları ayır
                raw_qs = re.split(r"\n(?=\d{1,3}[\.\)][\s\u00a0])", text)
                for q in raw_qs:
                    q = q.strip()
                    if len(q) < 30:
                        continue
                    fp = question_fingerprint(q)
                    if fp not in seen:
                        seen.add(fp)
                        questions.append({
                            "text": q,
                            "year": year,
                            "source": pdf_file.name,
                            "id": len(questions)
                        })
            
            if not questions:
                st.error("Soru bulunamadı! PDF formatını kontrol edin.")
            else:
                progress.progress(25)
                status.text(f"📚 {len(questions)} soru bulundu. Slaytlar okunuyor...")
                
                # 2. Slaytları oku
                lecture_dir = os.path.join(work_dir, "lectures")
                extract_zip(lectures_zip, lecture_dir)
                
                subjects = {}
                for folder in Path(lecture_dir).iterdir():
                    if folder.is_dir():
                        slides = []
                        for pdf_file in folder.rglob("*.pdf"):
                            doc = fitz.open(str(pdf_file))
                            for page_num in range(len(doc)):
                                page = doc[page_num]
                                text = page.get_text().strip()
                                if len(text) > 10:
                                    slides.append({
                                        "pdf_path": str(pdf_file),
                                        "page_num": page_num,
                                        "text": text[:800],
                                        "file_name": pdf_file.name
                                    })
                        if slides:
                            subjects[folder.name] = slides
                
                if not subjects:
                    st.error("Ders slaytı bulunamadı! ZIP içinde klasörler olmalı (Mikrobiyoloji/, Biyokimya/ vs)")
                else:
                    progress.progress(50)
                    status.text(f"🤖 {len(questions)} soru {len(subjects)} derse ayrılıyor...")
                    
                    # 3. Claude ile sınıflandır
                    client = anthropic.Anthropic(api_key=api_key)
                    classified = defaultdict(list)
                    
                    # Güvenlik için max 30 soru (ücretsiz token limiti için)
                    questions = questions[:30]
                    
                    batch_size = 3
                    for i in range(0, len(questions), batch_size):
                        batch = questions[i:i+batch_size]
                        numbered = "\n\n".join(f"[{j+1}] {q['text'][:400]}" for j, q in enumerate(batch))
                        
                        prompt = f"""Aşağıdaki TUS sorularını şu derslerden birine sınıflandır: {', '.join(subjects.keys())}
JSON formatında döndür: [{{"idx": 1, "ders": "DersAdı"}}]

Sorular:
{numbered}

SADECE JSON:"""
                        
                        try:
                            resp = client.messages.create(
                                model="claude-3-haiku-20240307",
                                max_tokens=300,
                                messages=[{"role": "user", "content": prompt}]
                            )
                            raw = resp.content[0].text
                            raw = re.sub(r"```[a-z]*", "", raw).strip()
                            assignments = json.loads(raw)
                            
                            for a in assignments:
                                idx = a.get("idx", 1) - 1
                                ders = a.get("ders", "")
                                if 0 <= idx < len(batch) and ders in subjects:
                                    classified[ders].append(batch[idx])
                                else:
                                    classified["Diğer"].append(batch[idx])
                        except Exception as e:
                            for q in batch:
                                classified["Diğer"].append(q)
                        
                        progress.progress(50 + (i/len(questions))*25)
                    
                    # 4. Soruları slaytlarla eşleştir ve göster
                    progress.progress(75)
                    status.text("📎 Slaytlarla eşleştiriliyor...")
                    
                    results = {}
                    for subject, qs in classified.items():
                        results[subject] = []
                        for q in qs:
                            slide = find_best_slide(q['text'], subjects.get(subject, []), api_key)
                            results[subject].append({
                                "question": q,
                                "slide": slide
                            })
                    
                    progress.progress(100)
                    status.text("✅ Tamamlandı!")
                    
                    # Sonuçları göster
                    st.success(f"{len(questions)} soru işlendi!")
                    
                    for subject, items in results.items():
                        with st.expander(f"📚 {subject} ({len(items)} soru)", expanded=True):
                            for item in items:
                                q = item["question"]
                                slide = item["slide"]
                                
                                st.markdown(f"**Soru ({q['year']}):** {q['text'][:300]}...")
                                
                                if slide:
                                    st.info(f"📖 İlgili Slayt: {slide['file_name']} - Sayfa {slide['page_num']+1}")
                                    
                                    # Slayt görüntüsünü göster (eğer mümkünse)
                                    img_data = get_pdf_page_image(slide['pdf_path'], slide['page_num'])
                                    if img_data:
                                        st.image(img_data, use_column_width=True)
                                    else:
                                        st.caption(f"Slayt metni: {slide['text'][:200]}...")
                                st.divider()
                    
                    # İndirilebilir rapor oluştur
                    report = []
                    for subject, items in results.items():
                        report.append(f"\n=== {subject} ===\n")
                        for item in items:
                            q = item["question"]
                            slide = item["slide"]
                            report.append(f"Soru ({q['year']}): {q['text'][:500]}...")
                            if slide:
                                report.append(f"Slayt: {slide['file_name']} s.{slide['page_num']+1}\n")
                    
                    txt_report = "\n".join(report)
                    st.download_button(
                        "📥 Raporu İndir (TXT)",
                        txt_report,
                        file_name="tus_eslestirme_raporu.txt",
                        mime="text/plain"
                    )

        except Exception as e:
            st.error(f"Hata oluştu: {str(e)}")
            st.exception(e)
