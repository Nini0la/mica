import {render,screen} from "@testing-library/react";
import {describe,expect,it} from "vitest";
import VocabularyHistory from "../app/vocabulary-history";


describe("VocabularyHistory",()=>{
  it("renders bounded vocabulary progress without hidden grading detail",()=>{
    render(<VocabularyHistory items={[{
      id:"word-1",display_word:"inchoate",definition:"only partly in existence; imperfectly formed",
      mastery_state:"developing",correct_recalls:1,recall_attempts:2,next_review_at:"2026-10-03T20:00:00Z",
    }]}/>);
    expect(screen.getByRole("heading",{name:"Vocabulary history"})).toBeInTheDocument();
    expect(screen.getByText("inchoate")).toBeInTheDocument();
    expect(screen.getByText(/^developing · 1 \/ 2 correct/)).toBeInTheDocument();
    expect(screen.queryByText(/reasoning/i)).toBeNull();
  });
});
