export type VocabularyHistoryItem={
  id:string;
  display_word:string;
  definition:string;
  mastery_state:"new"|"developing"|"familiar"|"mastered";
  correct_recalls:number;
  recall_attempts:number;
  next_review_at:string|null;
};

export default function VocabularyHistory({items}:{items:VocabularyHistoryItem[]}){
  if(items.length===0) return null;
  return <section aria-labelledby="vocabulary-history-heading">
    <h2 id="vocabulary-history-heading">Vocabulary history</h2>
    <ul>{items.map(item=><li key={item.id}>
      <strong>{item.display_word}</strong> — {item.definition}
      <small>{`${item.mastery_state} · ${item.correct_recalls} / ${item.recall_attempts} correct${item.next_review_at?` · next review ${item.next_review_at}`:""}`}</small>
    </li>)}</ul>
  </section>;
}
