import unittest

from archershub.course_search import CourseSearchResult, merge_revealed_teachers, search_courses


class CourseSearchHelperTests(unittest.TestCase):
    def test_course_matching_ranks_exact_and_prefix_first(self):
        courses = [
            CourseSearchResult("ABC101", "Intro", "1", "1", "2"),
            CourseSearchResult("LCFAITH", "Faith", "2", "1", "2"),
            CourseSearchResult("ZZZ", "LCFAITH Studies", "3", "1", "2"),
        ]
        self.assertEqual([c.course_code for c in search_courses(courses, "LCFAITH")], ["LCFAITH", "ZZZ"])
        self.assertEqual([c.course_code for c in search_courses(courses, "ABC")], ["ABC101"])

    def test_teacher_reveal_merges_schedule_rows_and_falls_back(self):
        sections = [
            {"course_creation_id": 10, "section_creation_id": 1, "batch_creation_id": 0, "section_name": "Z18", "main_teacher": "-"},
            {"course_creation_id": 10, "section_creation_id": 2, "batch_creation_id": 0, "section_name": "Z19", "main_teacher": "Existing"},
        ]
        schedule_rows = [
            {"COURSE_CREATION_ID": 10, "SECTION_CREATION_ID": 1, "BATCH_CREATION_ID": 0, "MAIN_TEACHER": "Prof X"},
            {"COURSE_CREATION_ID": 10, "SECTION_CREATION_ID": 2, "BATCH_CREATION_ID": 0, "MAIN_TEACHER": "Prof Y"},
        ]
        merged = merge_revealed_teachers(sections, schedule_rows)
        self.assertEqual(merged[0]["main_teacher"], "Prof X")
        self.assertEqual(merged[1]["main_teacher"], "Existing")

    def test_teacher_reveal_tolerates_missing_data(self):
        sections = [{"course_creation_id": 10, "section_creation_id": 1, "section_name": "Z18", "main_teacher": "-"}]
        self.assertEqual(merge_revealed_teachers(sections, None), sections)


if __name__ == "__main__":
    unittest.main()
