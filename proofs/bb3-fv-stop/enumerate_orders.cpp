// Enumerate every cyclic type word, using necessary entry/exit difference constraints.
// R=0, F=1, G=2. Start at an R to avoid a boundary FG swap.
#include <array>
#include <fstream>
#include <iostream>
#include <vector>
#include <algorithm>
#include <chrono>
using namespace std;
constexpr int N=17,P=1800;
int duration[3]={85,95,95};
int exit_h[3][3]={{61,-47,226},{186,78,351},{-67,-175,98}};
struct Edge {int a,b,c;};
array<int,N> word{};
long long examined=0,canonical_count=0,feasible_count=0;
ofstream output;
ofstream certificate;
bool canonical(){
 for(int k=1;k<N;k++) if(word[k]==0){
  for(int j=0;j<N;j++){
   if(word[j]<word[(j+k)%N])break;
   if(word[j]>word[(j+k)%N])return false;
  }
 }
 return true;
}
bool feasible(vector<Edge>& e,vector<int>& cycle){
 array<int,N> d{};
 array<int,N> predecessor{};
 int last=-1;
 for(int k=0;k<N;k++){
  bool changed=false;
  for(int j=0;j<(int)e.size();j++){
   auto x=e[j];
   if(d[x.b]<d[x.a]+x.c){d[x.b]=d[x.a]+x.c;changed=true;predecessor[x.b]=j;last=x.b;}
  }
  if(!changed)return true;
 }
 int node=last;
 for(int k=0;k<N;k++)node=e[predecessor[node]].a;
 int begin=node;
 do{int j=predecessor[node];cycle.push_back(j);node=e[j].a;}while(node!=begin);
 reverse(cycle.begin(),cycle.end());
 int total=0;for(int j:cycle)total+=e[j].c;
 if(total<=0)throw runtime_error("nonpositive purported certificate");
 return false;
}
void finish(){
 examined++;if(!canonical())return;canonical_count++;
 vector<int> exit;
 for(int i=0;i<N;i++){
  if(i+1<N && word[i]==1 && word[i+1]==2){exit.push_back(i+1);exit.push_back(i);i++;}
  else exit.push_back(i);
 }
 vector<Edge> edges;
 for(int i=0;i<N;i++)edges.push_back({i,(i+1)%N,duration[word[i]]-(i==N-1?P:0)});
 for(int k=0;k<N;k++){
  int i=exit[k],j=exit[(k+1)%N];
  edges.push_back({i,j,exit_h[word[i]][word[j]]-(k==N-1?P:0)});
 }
 vector<int> cycle;
 bool ok=feasible(edges,cycle);
 for(int x:word)certificate<<"RFG"[x];certificate<<'\t';
 if(ok){
  certificate<<"PASS\t0\n";
  feasible_count++;
  for(int x:word)output<<"RFG"[x];output<<'\n';
 }else{
  int total=0;
  for(int k=0;k<(int)cycle.size();k++){
   if(k)certificate<<',';certificate<<cycle[k];total+=edges[cycle[k]].c;
  }
  certificate<<'\t'<<total<<'\n';
 }
}
void walk(int pos,int r,int f,int g){
 if(pos==N){finish();return;}
 if(r){word[pos]=0;walk(pos+1,r-1,f,g);}
 if(f){word[pos]=1;walk(pos+1,r,f-1,g);}
 if(g){word[pos]=2;walk(pos+1,r,f,g-1);}
}
int main(int argc,char**argv){
 auto start=chrono::steady_clock::now();
 output.open(argc>1?argv[1]:"surviving-type-orders.txt");
 certificate.open(argc>2?argv[2]:"order-certificates.tsv");
 word[0]=0;walk(1,6,7,3);output.close();certificate.close();
 cout<<"{\"examined\":"<<examined<<",\"canonical\":"<<canonical_count<<",\"survivors\":"<<feasible_count<<",\"seconds\":"<<chrono::duration<double>(chrono::steady_clock::now()-start).count()<<"}"<<endl;
}
