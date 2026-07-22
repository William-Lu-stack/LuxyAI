import io
import unittest
import zipfile

from fastapi import HTTPException

from backend.app.services.knowledge_files import extract_knowledge_file


class KnowledgeFileExtractionTests(unittest.TestCase):
    def test_extracts_docx_text(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>CrashLoop Runbook</w:t></w:r></w:p><w:p><w:r><w:t>Check previous logs.</w:t></w:r></w:p></w:body></w:document>""",
            )
        content, document_type = extract_knowledge_file(buffer.getvalue(), "runbook.docx")
        self.assertEqual(document_type, "docx")
        self.assertIn("CrashLoop Runbook", content)
        self.assertIn("Check previous logs", content)

    def test_html_drops_script_content(self):
        content, document_type = extract_knowledge_file(
            b"<h1>Safe title</h1><script>leaked_secret()</script><p>Runbook body</p>",
            "guide.html",
        )
        self.assertEqual(document_type, "html")
        self.assertIn("Safe title", content)
        self.assertIn("Runbook body", content)
        self.assertNotIn("leaked_secret", content)

    def test_rejects_unknown_binary_type(self):
        with self.assertRaises(HTTPException) as raised:
            extract_knowledge_file(b"binary", "payload.exe")
        self.assertEqual(raised.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
