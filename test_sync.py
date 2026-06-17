"""Test sync-first cabinet flow."""
import sys, json, time
sys.path.insert(0, r"C:\Users\sorivma\Desktop\01_Projects\learning\miit-timetable-mcp")
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

# Step 3: Grades (cache)
print("\n3. GRADES (cache)...")
t0 = time.time()
result = cab.get_grades()
t1 = time.time()
print(f"   Done in {t1-t0:.3f}s — {result['total']} grades")
for g in result.get("grades", [])[:3]:
    print(f"   [{g.get('semester','?')}й сем.] {g.get('discipline','?')[:40]} | {g.get('type','?')} | {g.get('grade','?')}")

# Step 4: Filtered grades
print("\n4. GRADES (filtered: semester=2)...")
result = cab.get_grades(semester="2")
print(f"   {result['total']} grades in semester 2")
for g in result.get("grades", [])[:3]:
    print(f"   {g.get('discipline','?')[:40]} | {g.get('type','?')} | {g.get('grade','?')} | {g.get('teacher','?')[:30]}")

# Step 5: Portfolio
print("\n5. PORTFOLIO (cache)...")
result = cab.get_portfolio()
print(f"   {result}")

# Step 6: Cache file exists?
import os
cache_path = os.path.join(os.path.dirname(cab.__file__) if hasattr(cab, '__file__') else r"C:\Users\sorivma\Desktop\01_Projects\learning\miit-timetable-mcp", "cabinet_cache.json")
if os.path.exists(cache_path):
    size = os.path.getsize(cache_path)
    print(f"\n6. CACHE FILE: {cache_path} ({size} bytes)")
else:
    print(f"\n6. CACHE FILE: not found at {cache_path}")
