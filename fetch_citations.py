#!/usr/bin/env python3
"""
Fetch citation counts from NASA ADS for all publications.
Run daily via GitHub Actions. Outputs citations.json.
"""
import json
import requests
import os
import sys
import time
from datetime import datetime

ADS_TOKEN = os.environ.get("ADS_API_TOKEN")
if not ADS_TOKEN:
    print("ERROR: ADS_API_TOKEN environment variable not set")
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {ADS_TOKEN}"}

FIRST_AUTHOR = {
    "17": "2026ApJ...996L..22P",
    "16": "2024ApJ...973L..45P",
    "15": "2024A&A...685A.115P",
    "14": "2023ApJ...958...28P",
    "13": "2023ApJ...958...27P",
    "12": "2022Galax..10..102P",
    "11": "2021ApJ...922..180P",
    "10": "2021ApJ...909...76P",
    "9":  "2021ApJ...906...85P",
    "8":  "2019ApJ...887..147P",
    "7":  "2019ApJ...877..106P",
    "6":  "2019ApJ...871..257P",
    "5":  "2018ApJ...860..112P",
    "4":  "2017ApJ...834..157P",
    "3":  "2015A&A...576L..16P",
    "2":  "2014ApJ...785...76P",
    "1":  "2012JKAS...45..147P",
}

STUDENT = {"s1": "2024A&A...688A..94Y"}

COAUTHOR = {
    "78": "2025A&A...699A.341E", "77": "2025A&A...699A.279D",
    "76": "2025A&A...699A.265G", "75": "2025ApJ...986...49K",
    "74": "2025A&A...695A.233R", "73": "2025A&A...694A.291K",
    "72": "2025JKAS...58...17C", "71": "2025A&A...693A.265E",
    "70": "2024A&A...692A.205B", "69": "2024A&A...692A.140A",
    "68": "2024ApJ...973..100K", "67": "2024AJ....168..130R",
    "66": "2024ApJ...970..176K", "65": "2024ApJ...964L..26E",
    "64": "2024ApJ...964L..25E", "63": "2024A&A...682L...3P",
    "62": "2024A&A...681A..79E", "61": "2023ApJ...958..143T",
    "60": "2023ApJ...957L..21R", "59": "2023ApJ...957L..20E",
    "58": "2023PASP..135i5001C", "57": "2023Natur.621..711C",
    "56": "2023MNRAS.523.5703J", "55": "2023ApJ...952...47T",
    "54": "2023ApJ...950...35P", "53": "2023ApJ...950...10L",
    "52": "2023A&A...673A.159R", "51": "2023Natur.616..686L",
    "50": "2023ApJ...943..170J", "49": "2023JKAS...56....1K",
    "48": "2022Galax..10..113A", "47": "2022ApJ...939...83K",
    "46": "2022ApJ...935...61B", "45": "2022ApJ...934..145I",
    "44": "2022ApJ...932...72Z", "43": "2022ApJ...930L..21B",
    "42": "2022ApJ...930L..20G", "41": "2022ApJ...930L..19W",
    "40": "2022ApJ...930L..18F", "39": "2022ApJ...930L..17E",
    "38": "2022ApJ...930L..16E", "37": "2022ApJ...930L..15E",
    "36": "2022ApJ...930L..14E", "35": "2022ApJ...930L..13E",
    "34": "2022ApJ...930L..12E", "33": "2022ApJ...926..108C",
    "32": "2021RAA....21..205C", "31": "2021NatAs...5.1017J",
    "30": "2021ApJ...914...43H", "29": "2021A&A...651A..74K",
    "28": "2021PhRvD.103j4047K", "27": "2021ApJ...912...35N",
    "26": "2021RAA....21..205C", "25": "2021ApJ...911L..11E",
    "24": "2021ApJ...910L..14G", "23": "2021ApJ...910L..13E",
    "22": "2021ApJ...910L..12E", "21": "2020PhRvL.125n1104P",
    "20": "2020ApJ...902..104L", "19": "2020ApJ...901...67W",
    "18": "2020A&A...640A..69K", "17c": "2020ApJ...897..148G",
    "16c": "2020ApJ...897..139B", "15c": "2020A&A...637L...6K",
    "14c": "2019ApJ...886...85A", "13c": "2019MNRAS.486.2412L",
    "12c": "2019JKAS...52...23Z", "11c": "2018MNRAS.480.2324K",
    "10c": "2018ApJ...859..128A", "9c": "2018MNRAS.475..368H",
    "8c": "2018ApJ...852...30A", "7c": "2018AJ....155...26Z",
    "6c": "2017JKAS...50..167K", "5c": "2017PASJ...69...71H",
    "4c": "2016ApJS..227....8L", "3c": "2015JKAS...48..299O",
    "2c": "2015JKAS...48..285K", "1c": "2015JKAS...48..237A",
}

def fetch_citations(bibcodes_dict, label=""):
    results = {}
    bibcodes = list(set(bibcodes_dict.values()))
    
    for i in range(0, len(bibcodes), 20):
        batch = bibcodes[i:i+20]
        query = " OR ".join([f'bibcode:"{b}"' for b in batch])
        
        try:
            resp = requests.get(
                "https://api.adsabs.harvard.edu/v1/search/query",
                headers=HEADERS,
                params={"q": query, "fl": "bibcode,citation_count", "rows": len(batch)}
            )
            resp.raise_for_status()
            data = resp.json()
            
            bib_to_cite = {}
            for doc in data.get("response", {}).get("docs", []):
                bib_to_cite[doc["bibcode"]] = doc.get("citation_count", 0)
            
            for pid, bib in bibcodes_dict.items():
                if bib in bib_to_cite and pid not in results:
                    results[pid] = bib_to_cite[bib]
            
            print(f"  [{label}] Batch {i//20+1}: queried {len(batch)}, got {len(bib_to_cite)}")
            
        except Exception as e:
            print(f"  [{label}] Error batch {i//20+1}: {e}")
        
        time.sleep(0.3)
    
    return results

def main():
    print("Fetching first-author citations...")
    first = fetch_citations(FIRST_AUTHOR, "1st")
    print(f"  => {len(first)}/{len(FIRST_AUTHOR)} papers\n")
    
    print("Fetching student citations...")
    student = fetch_citations(STUDENT, "stu")
    print(f"  => {len(student)}/{len(STUDENT)} papers\n")
    
    print("Fetching co-author citations...")
    coauth = fetch_citations(COAUTHOR, "co")
    print(f"  => {len(coauth)}/{len(COAUTHOR)} papers\n")
    
    output = {
        "first_author": first,
        "student": student,
        "coauthor": coauth,
        "updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    }
    
    with open("citations.json", "w") as f:
        json.dump(output, f, indent=2)
    
    total = sum(first.values()) + sum(student.values()) + sum(coauth.values())
    print(f"Done! Total citations: {total}")

if __name__ == "__main__":
    main()
