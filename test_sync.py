"""Test sync-first cabinet flow."""
import sys, json, time
sys.path.insert(0, r"C:\Users\sorivma\Desktop\01_Projects\ai\miit-timetable-mcp")
import cabinet as cab

# Step 1: Sync (opens browser)
print("1. SYNC (browser)...")
t0 = time.time()
result = cab.sync_cabinet()
t1 = time.time()
print(f"   Done in {t1-t0:.1f}s: {json.dumps(result, ensure_ascii=False, indent=2)}")

# Step 2: Read from cache (no browser)
print("\n2. PROFILE (cache)...")
t0 = time.time()
result = cab.get_profile()
t1 = time.time()
print(f"   Done in {t1-t0:.3f}s")
prof = result.get("profile", {})
for k, v in prof.items():
    print(f"   {k}: {v}")

# Step 3: Grades
print("\n3. GRADES (cache)...")
t0 = time.time()
result = cab.get_grades()
t1 = time.time()
print(f"   Done in {t1-t0:.3f}s — {result['total']} grades")
for g in result.get("grades", [])[:3]:
    print(f"   [{g.get('semester','?')}й сем.] {g.get('discipline','?')[:40]} | {g.get('type','?')} | {g.get('grade','?')}")

# Step 4: Filtered grades
print("\n4. GRADES (semester=2)...")
result = cab.get_grades(semester="2")
print(f"   {result['total']} grades in semester 2")
for g in result.get("grades", [])[:3]:
    print(f"   {g.get('discipline','?')[:40]} | {g.get('type','?')} | {g.get('grade','?')} | {g.get('teacher','?')[:30]}")

# Step 5: Disciplines
print("\n5. DISCIPLINES (cache)...")
result = cab.get_disciplines()
print(f"   {result['total']} disciplines")
for d in result.get("disciplines", [])[:3]:
    print(f"   [{d.get('semester','?')}] {d['discipline'][:40]} | dept: {d.get('department','?')} | docs: {len(d.get('documents',[]))}")

# Step 6: Study plan
print("\n6. STUDY PLAN (cache)...")
result = cab.get_study_plan()
sp = result.get("study_plan", {})
for k, v in sp.items():
    if k != "documents":
        print(f"   {k}: {v}")
print(f"   documents: {len(sp.get('documents', []))}")

# Step 7: Portfolio
print("\n7. PORTFOLIO (cache)...")
result = cab.get_portfolio()
print(f"   {result['total']} items")

# Step 8: Education
print("\n8. EDUCATION (cache)...")
result = cab.get_education()
print(f"   {len(result.get('options',[]))} options")
print(f"   current: {(result.get('current') or {}).get('text', 'N/A')}")

# Step 9: Cache file
import os
cache_path = r"C:\Users\sorivma\Desktop\01_Projects\ai\miit-timetable-mcp\cabinet_cache.json"
if os.path.exists(cache_path):
    size = os.path.getsize(cache_path)
    print(f"\n9. CACHE: {cache_path} ({size} bytes)")
