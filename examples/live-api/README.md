# Run the API examples

Download `examples.zip` from the guide and unzip it. Open a terminal in the extracted folder.
Install Python and httpx (`python -m pip install httpx`). Get an API token from the operator.

```powershell
$env:OCR_API_URL = "https://pravdokapi.jerteh.rs"
$env:OCR_API_TOKEN = "<your token>"
python docs/examples/live-api/run_examples.py
```

The script runs scan OCR, Serbian-to-English and English-to-Serbian translation,
then a PDF translation job. It creates one job and saves fresh results beside
the script in `results/`. Reruns replace those named result files.

The guide shows saved responses from fictional test documents. Review results
for OCR/translation errors and PDF rendering defects before use.
