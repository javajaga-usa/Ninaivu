export class History {
  constructor(initial) { this.items = [{...initial}]; this.index = 0; }
  get current() { return {...this.items[this.index]}; }
  push(value) {
    this.items.splice(this.index + 1);
    this.items.push({...value});
    if (this.items.length > 30) this.items.shift();
    this.index = this.items.length - 1;
  }
  undo() { this.index = Math.max(0, this.index-1); return this.current; }
  redo() { this.index = Math.min(this.items.length-1, this.index+1); return this.current; }
}
